"""
chat app Model 测试 —— Session / QaRecord / QaFeedback

覆盖范围：
- Session：默认值创建（root_type/turn_count/is_archived/last_active_at）、软删除标记、字符串表示
- QaRecord：默认值创建、字符串表示、feedback 关联
- QaFeedback：创建默认值、qa_record 一对一唯一约束、字符串表示

说明：Session 定义在 memory app（apps.memory.models.Session），chat 通过外键引用。
软删除采用 is_deleted 标记位（SessionViewSet.destroy 只置位不物理删除）。
"""
import pytest
from django.db import IntegrityError

from apps.users.models import User
from apps.memory.models import Session
from apps.chat.models import QaRecord, QaFeedback, HotQaCache, TaskDecomposition


@pytest.mark.django_db
class TestSessionModel:
    """Session 模型测试（模型本体位于 apps.memory.models）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入测试用户"""
        self.user = User.objects.create_user(
            username='chat-model-user', email='chat-model@test.com', password='testpass123')

    def test_create_with_defaults(self):
        """创建会话应带默认值：root_type=company_doc、turn_count=0、is_archived=False、is_deleted=False、title=新会话"""
        s = Session.objects.create(user=self.user)
        assert s.root_type == 'company_doc'
        assert s.turn_count == 0
        assert not s.is_archived
        assert not s.is_deleted
        assert s.title == '新会话'
        # last_active_at 由 auto_now 自动写入，创建后不应为空
        assert s.last_active_at is not None
        assert s.created_at is not None

    def test_soft_delete(self):
        """软删除：仅置位 is_deleted=True，记录仍保留在库中"""
        s = Session.objects.create(user=self.user, title='待删除会话')
        s.is_deleted = True
        s.save(update_fields=['is_deleted'])
        s.refresh_from_db()
        assert s.is_deleted
        # 软删除不物理删除，仍可查询到
        assert Session.objects.filter(id=s.id).exists()

    def test_str(self):
        """字符串表示格式：Sess<id>title"""
        s = Session.objects.create(user=self.user, title='报销流程')
        assert str(s) == f'Sess<{s.id}>报销流程'


@pytest.mark.django_db
class TestQaRecordModel:
    """QaRecord 模型测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户与会话"""
        self.user = User.objects.create_user(
            username='qa-model-user', email='qa-model@test.com', password='testpass123')
        self.session = Session.objects.create(user=self.user, title='测试会话')

    def test_create_with_defaults(self):
        """创建问答记录应带默认值：answer 空、answer_type=rag、root_type=company_doc、is_success=True"""
        qa = QaRecord.objects.create(session=self.session, user=self.user, question='什么是 RAG？')
        assert qa.answer == ''
        assert qa.answer_type == 'rag'
        assert qa.root_type == 'company_doc'
        assert qa.is_success
        assert not qa.is_hit_cache
        assert qa.turn_index == 0
        assert qa.uuid is not None

    def test_str(self):
        """字符串表示格式：QA<id> + question 前 20 字符"""
        question = '这是一个超过二十个字符的测试问题内容'
        qa = QaRecord.objects.create(session=self.session, user=self.user, question=question)
        assert str(qa) == f'QA<{qa.id}>' + question[:20]

    def test_feedback_relationship(self):
        """反馈通过 related_name='feedback' 一对一关联问答记录"""
        qa = QaRecord.objects.create(session=self.session, user=self.user, question='测试问题')
        fb = QaFeedback.objects.create(qa_record=qa, user=self.user, rating=1)
        assert qa.feedback == fb
        assert fb.qa_record == qa


@pytest.mark.django_db
class TestQaFeedbackModel:
    """QaFeedback 模型测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/会话/QA 记录"""
        self.user = User.objects.create_user(
            username='fb-model-user', email='fb-model@test.com', password='testpass123')
        self.session = Session.objects.create(user=self.user, title='测试会话')
        self.qa = QaRecord.objects.create(session=self.session, user=self.user, question='测试问题')

    def test_create_with_defaults(self):
        """创建反馈应带默认值：rating=0（中性）、status=pending、tags 空列表"""
        fb = QaFeedback.objects.create(qa_record=self.qa, user=self.user)
        assert fb.rating == 0
        assert fb.status == 'pending'
        assert fb.tags == []
        assert fb.comment == ''

    def test_unique_qa_record_constraint(self):
        """qa_record 一对一唯一：同一问答记录重复创建反馈应抛 IntegrityError"""
        QaFeedback.objects.create(qa_record=self.qa, user=self.user, rating=1)
        with pytest.raises(IntegrityError):
            QaFeedback.objects.create(qa_record=self.qa, user=self.user, rating=-1)

    def test_str(self):
        """模型未自定义 __str__，使用 Django 默认对象表示"""
        fb = QaFeedback.objects.create(qa_record=self.qa, user=self.user)
        assert 'QaFeedback object' in str(fb)


@pytest.mark.django_db
class TestHotQaCacheModel:
    """HotQaCache 热点问答缓存模型测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入测试用户"""
        self.user = User.objects.create_user(
            username='hot-model-user', email='hot-model@test.com', password='testpass123')

    def test_create_with_defaults(self):
        """创建缓存应带默认值：hit_count=0、visibility_scope=public、citations 空列表"""
        cache = HotQaCache.objects.create(
            question_hash='abc123', root_type='company_doc',
            question='什么是 RAG？', answer='RAG 是检索增强生成。')
        assert cache.hit_count == 0
        assert cache.visibility_scope == 'public'
        assert cache.citations == []
        assert cache.cited_doc_ids == []
        assert cache.last_hit_at is not None

    def test_unique_together_constraint(self):
        """question_hash+root_type+visibility_scope 唯一：重复创建应抛 IntegrityError"""
        HotQaCache.objects.create(
            question_hash='abc123', root_type='company_doc',
            question='q1', answer='a1')
        with pytest.raises(IntegrityError):
            HotQaCache.objects.create(
                question_hash='abc123', root_type='company_doc',
                question='q2', answer='a2')

    def test_same_hash_different_scope_allowed(self):
        """相同 question_hash 但不同 visibility_scope 可并存"""
        HotQaCache.objects.create(
            question_hash='abc123', root_type='company_doc',
            question='q1', answer='a1', visibility_scope='public')
        HotQaCache.objects.create(
            question_hash='abc123', root_type='company_doc',
            question='q2', answer='a2', visibility_scope='private')
        assert HotQaCache.objects.filter(question_hash='abc123').count() == 2


@pytest.mark.django_db
class TestTaskDecompositionModel:
    """TaskDecomposition 复杂任务拆分记录模型测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/会话/QA 记录"""
        self.user = User.objects.create_user(
            username='td-model-user', email='td-model@test.com', password='testpass123')
        self.session = Session.objects.create(user=self.user, title='测试会话')
        self.qa = QaRecord.objects.create(
            session=self.session, user=self.user, question='复杂问题', answer='合并答案')

    def test_create_with_defaults(self):
        """创建拆分记录应带默认值：status=planning、sub_tasks 空列表"""
        td = TaskDecomposition.objects.create(
            qa_record=self.qa, original_question='复杂问题')
        assert td.status == 'planning'
        assert td.sub_tasks == []
        assert td.merged_answer == ''
        assert td.total_latency_ms == 0

    def test_status_transition(self):
        """状态可从 planning 流转到 done"""
        td = TaskDecomposition.objects.create(
            qa_record=self.qa, original_question='复杂问题',
            sub_tasks=[{'index': 1, 'question': '子任务', 'answer': '子答案'}])
        td.status = 'done'
        td.merged_answer = '合并后的完整答案'
        td.save(update_fields=['status', 'merged_answer'])
        td.refresh_from_db()
        assert td.status == 'done'
        assert td.merged_answer == '合并后的完整答案'

    def test_qa_record_one_to_one(self):
        """qa_record 一对一：同一问答记录重复创建拆分记录应抛 IntegrityError"""
        TaskDecomposition.objects.create(qa_record=self.qa, original_question='复杂问题')
        with pytest.raises(IntegrityError):
            TaskDecomposition.objects.create(qa_record=self.qa, original_question='重复')
