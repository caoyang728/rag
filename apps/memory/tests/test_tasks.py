"""
apps.memory.tasks 测试 —— 会话/用户记忆提炼 Celery 任务

覆盖范围：
- refine_session_memory：会话不存在 / 无 QA 记录 / 提炼成功写入 SessionMemory / 全部重试失败降级
- refine_user_memory：指定用户 / 昨日无新对话跳过 / 提炼成功更新 UserMemory

采用 DB 集成 + mock LLM：
任务逻辑依赖 Session/QaRecord/UserMemory 等真实表（created_at 窗口过滤、update_or_create），
故保留 ORM；LLM 调用通过 patch apps.memory.tasks.llm_with_retry 与 get_llm 隔离，
避免真实网络与 token 消耗。测试设置中 Celery 同步执行，直接调用任务函数即可。
"""
from datetime import timedelta

import pytest
from unittest.mock import MagicMock, patch

from django.utils import timezone

from apps.memory.models import Session, SessionMemory, UserMemory
from apps.memory.tasks import refine_session_memory, refine_user_memory
from apps.chat.models import QaRecord
from apps.users.models import User


def _make_user(username='mem-task-user'):
    """创建测试用户（信号会自动初始化 UserMemory）"""
    return User.objects.create_user(
        username=username, email=f'{username}@test.com', password='testpass123')


def _make_session(user, title='测试会话'):
    """创建会话"""
    return Session.objects.create(user=user, title=title)


def _make_qa(session, user, question, answer, turn_index=0, created_at=None):
    """创建 QA 记录，created_at 通过 queryset.update 回填（auto_now_add 不可直接赋值）"""
    qa = QaRecord.objects.create(
        session=session, user=user, turn_index=turn_index,
        question=question, answer=answer)
    if created_at:
        QaRecord.objects.filter(id=qa.id).update(created_at=created_at)
        qa.refresh_from_db()
    return qa


class _Result:
    """模拟 llm_with_retry 返回的结构化结果对象"""

    def __init__(self, summary='摘要', entities=None, keywords=None,
                 domain_tags=None, frequent_topics=None, preferences=None, profile_text=None):
        self.summary = summary
        self.entities = entities or ['实体A']
        self.keywords = keywords or ['关键词A']
        self.domain_tags = domain_tags or ['技术']
        self.frequent_topics = frequent_topics or ['主题A']
        self.preferences = preferences or {'风格': '简洁'}
        self.profile_text = profile_text or '用户画像文本'


# ============================================================================
# refine_session_memory
# ============================================================================
@pytest.mark.django_db
@pytest.mark.integration
class TestRefineSessionMemory:
    """会话摘要提炼任务测试"""

    def test_session_not_found(self):
        """会话不存在时返回失败结果，不抛异常"""
        result = refine_session_memory(99999)
        assert result == {'ok': False, 'error': 'session not found'}

    def test_no_qa_skips(self):
        """会话无 QA 记录时返回 reason='no qa'"""
        user = _make_user()
        sess = _make_session(user)
        with patch('apps.llm.factory.get_llm') as mock_get_llm:
            result = refine_session_memory(sess.id)
        assert result == {'ok': True, 'reason': 'no qa'}
        mock_get_llm.assert_not_called()

    def test_success_writes_session_memory(self):
        """提炼成功：SessionMemory 写入 summary/entities/keywords 并记录 turn_refined"""
        user = _make_user()
        sess = _make_session(user)
        sess.turn_count = 12
        sess.save(update_fields=['turn_count'])
        _make_qa(sess, user, '问题1', '回答1', turn_index=0)
        _make_qa(sess, user, '问题2', '回答2', turn_index=1)

        with patch('apps.memory.tasks.llm_with_retry',
                   return_value=_Result('会话摘要', ['实体'], ['关键词'])) as mock_retry, \
                patch('apps.llm.factory.get_llm', return_value=MagicMock()):
            result = refine_session_memory(sess.id)

        assert result == {'ok': True, 'session_id': sess.id}
        mock_retry.assert_called_once()
        sm = SessionMemory.objects.get(session=sess)
        assert sm.summary == '会话摘要'
        assert sm.entities == ['实体']
        assert sm.keywords == ['关键词']
        assert sm.turn_refined == 12

    def test_all_retries_failed_fallback(self):
        """全部重试失败：写入降级占位摘要，任务仍返回 ok"""
        user = _make_user()
        sess = _make_session(user)
        _make_qa(sess, user, '问题1', '回答1')

        with patch('apps.memory.tasks.llm_with_retry', return_value=None), \
                patch('apps.llm.factory.get_llm', return_value=MagicMock()):
            result = refine_session_memory(sess.id)

        assert result['ok'] is True
        sm = SessionMemory.objects.get(session=sess)
        assert sm.summary == '（提炼失败）'
        assert sm.entities == []
        assert sm.keywords == []


# ============================================================================
# refine_user_memory
# ============================================================================
@pytest.mark.django_db
@pytest.mark.integration
class TestRefineUserMemory:
    """用户画像增量提炼任务测试"""

    @staticmethod
    def _yesterday():
        """返回任务"昨日窗口"内的时刻

        任务窗口 = [make_aware(今日0点) - 24h, make_aware(今日0点))。
        用与任务相同的基准再减 12 小时得到"昨日正午"，无论当前几点都必然落在窗口内，
        避免用 now()-24h 在跨日/跨时区边界时跑出窗口导致 updated=0。
        """
        today = timezone.now().date()
        return timezone.make_aware(
            timezone.datetime(today.year, today.month, today.day)) - timedelta(hours=12)

    def test_specified_user_without_yesterday_qa_skipped(self):
        """指定用户但昨日无新对话：不调用 LLM，updated=0"""
        user = _make_user()
        with patch('apps.llm.factory.get_llm') as mock_get_llm:
            result = refine_user_memory(user_id=user.id)
        assert result == {'ok': True, 'updated': 0}
        mock_get_llm.assert_not_called()

    def test_success_updates_user_memory(self):
        """昨日有对话且提炼成功：更新画像字段并计数 updated=1"""
        user = _make_user()
        sess = _make_session(user)
        _make_qa(sess, user, '问题1', '回答1', created_at=self._yesterday())

        with patch('apps.memory.tasks.llm_with_retry', return_value=_Result(
                domain_tags=['技术'], frequent_topics=['主题'], preferences={'风格': '简洁'},
                profile_text='画像文本')) as mock_retry, \
                patch('apps.llm.factory.get_llm', return_value=MagicMock()):
            result = refine_user_memory(user_id=user.id)

        assert result == {'ok': True, 'updated': 1}
        mock_retry.assert_called_once()
        um = UserMemory.objects.get(user=user)
        assert um.domain_tags == ['技术']
        assert um.frequent_topics == ['主题']
        assert um.preferences == {'风格': '简洁'}
        assert um.profile_text == '画像文本'
        assert um.session_refined_count == 1

    def test_retry_failed_keeps_existing_profile(self):
        """LLM 全部失败：保留已有画像，updated=0"""
        user = _make_user()
        sess = _make_session(user)
        _make_qa(sess, user, '问题1', '回答1', created_at=self._yesterday())
        um = UserMemory.objects.get(user=user)
        um.profile_text = '已有画像'
        um.save()

        with patch('apps.memory.tasks.llm_with_retry', return_value=None), \
                patch('apps.llm.factory.get_llm', return_value=MagicMock()):
            result = refine_user_memory(user_id=user.id)

        assert result == {'ok': True, 'updated': 0}
        um.refresh_from_db()
        assert um.profile_text == '已有画像'
        assert um.session_refined_count == 0
