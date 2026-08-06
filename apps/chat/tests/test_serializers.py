"""
chat app Serializer 测试 —— SessionSerializer / QaRecordSerializer / QaFeedbackSerializer

覆盖范围：
- SessionSerializer：字段输出、preview 首问逻辑、read_only 字段、部分更新（partial）
- QaRecordSerializer：字段输出、answer_type 枚举校验、必填字段、session_id 只读语义、部分更新
- QaFeedbackSerializer：rating 类型校验、qa_record_id 只读语义、status 只读、部分更新

说明：
- root_type 在 Session/QaRecord 模型上均为自由字符串字段（无 choices），序列化器不做枚举限制。
- FK 的 attname（如 session_id/qa_record_id）被 DRF 映射为只读字段，创建时须在 save() 中显式注入。
- QaFeedback.rating 的 RATING_CHOICES 常量未挂到字段定义上，故序列化器层无枚举校验。
"""
import pytest

from apps.users.models import User
from apps.memory.models import Session
from apps.chat.models import QaRecord, QaFeedback
from apps.chat.serializers import (
    SessionSerializer, QaRecordSerializer, QaFeedbackSerializer,
)


@pytest.mark.django_db
class TestSessionSerializer:
    """SessionSerializer 测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户与会话"""
        self.user = User.objects.create_user(
            username='ser-sess-user', email='ser-sess@test.com', password='testpass123')
        self.session = Session.objects.create(
            user=self.user, title='会话标题', root_type='company_doc')

    def test_serialize_fields(self):
        """序列化输出应包含约定字段，无首问注解时 preview 为空串"""
        data = SessionSerializer(self.session).data
        for field in ['id', 'title', 'root_type', 'is_archived', 'turn_count',
                      'last_active_at', 'created_at', 'preview']:
            assert field in data
        assert data['preview'] == ''

    def test_preview_long_question_truncated(self):
        """有 _first_question 注解且超 50 字符时 preview 截断并追加 ..."""
        s = Session.objects.create(user=self.user, title='带首问')
        s._first_question = '这是一段用于测试预览截断逻辑的很长的问题文本内容' * 3  # 72 字符，超 50
        data = SessionSerializer(s).data
        assert data['preview'].endswith('...')
        assert len(data['preview']) <= 53

    def test_preview_short_question(self):
        """首问不超过 50 字符时 preview 原样返回"""
        s = Session.objects.create(user=self.user, title='带短首问')
        s._first_question = '报销流程是什么'
        assert SessionSerializer(s).data['preview'] == '报销流程是什么'

    def test_create_with_defaults(self):
        """空数据创建应带模型默认值（title=新会话、root_type=company_doc）"""
        serializer = SessionSerializer(data={})
        assert serializer.is_valid()
        s = serializer.save(user=self.user)
        assert s.title == '新会话'
        assert s.root_type == 'company_doc'

    def test_root_type_custom_value(self):
        """root_type 为自由字符串，任意非空值应通过校验"""
        serializer = SessionSerializer(data={'title': 'x', 'root_type': 'wiki_doc'})
        assert serializer.is_valid()

    def test_read_only_fields_ignored(self):
        """turn_count/last_active_at/created_at 为只读，创建时传入值应被忽略"""
        serializer = SessionSerializer(data={
            'title': 't', 'turn_count': 99, 'last_active_at': '2020-01-01T00:00:00Z',
        })
        assert serializer.is_valid()
        s = serializer.save(user=self.user)
        assert s.turn_count == 0  # 未被外部值覆盖

    def test_partial_update(self):
        """部分更新仅修改传入字段，其余字段保留原值"""
        serializer = SessionSerializer(
            self.session, data={'title': '新标题'}, partial=True)
        assert serializer.is_valid()
        s = serializer.save()
        assert s.title == '新标题'
        assert s.root_type == 'company_doc'  # 未修改字段保持原值


@pytest.mark.django_db
class TestQaRecordSerializer:
    """QaRecordSerializer 测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/会话/QA 记录"""
        self.user = User.objects.create_user(
            username='ser-qa-user', email='ser-qa@test.com', password='testpass123')
        self.session = Session.objects.create(user=self.user, title='测试会话')
        self.qa = QaRecord.objects.create(
            session=self.session, user=self.user, question='问题', answer='答案')

    def test_serialize_fields(self):
        """序列化输出应包含关键字段，无工具调用链时 tool_traces 为空列表"""
        data = QaRecordSerializer(self.qa).data
        for field in ['id', 'uuid', 'session_id', 'turn_index', 'question', 'answer',
                      'answer_type', 'citations', 'retrieval_hits', 'tool_traces', 'created_at']:
            assert field in data
        assert data['tool_traces'] == []

    def test_answer_type_valid_choices(self):
        """四种合法 answer_type 均应通过校验"""
        for at in ['rag', 'reasoning', 'mixed', 'refused']:
            serializer = QaRecordSerializer(
                data={'session_id': self.session.id, 'question': 'q', 'answer_type': at})
            assert serializer.is_valid()

    def test_answer_type_invalid(self):
        """非法 answer_type 应校验失败"""
        serializer = QaRecordSerializer(
            data={'session_id': self.session.id, 'question': 'q', 'answer_type': 'invalid_type'})
        assert not serializer.is_valid()
        assert 'answer_type' in serializer.errors

    def test_question_required(self):
        """question 为必填字段"""
        serializer = QaRecordSerializer(data={'session_id': self.session.id})
        assert not serializer.is_valid()
        assert 'question' in serializer.errors

    def test_session_id_read_only(self):
        """session_id 为只读字段（模型 FK 的 attname 被 DRF 映射为只读属性），输入值不进入 validated_data"""
        serializer = QaRecordSerializer(
            data={'session_id': self.session.id, 'question': 'q'})
        assert serializer.is_valid()
        assert 'session_id' not in serializer.validated_data

    def test_create_with_explicit_session(self):
        """创建时需通过 save(session=...) 显式注入会话"""
        serializer = QaRecordSerializer(
            data={'question': 'q', 'answer': 'a', 'answer_type': 'rag'})
        assert serializer.is_valid()
        qa = serializer.save(session=self.session)
        assert qa.session == self.session
        assert qa.question == 'q'

    def test_partial_update(self):
        """部分更新仅修改传入字段，其余字段保留原值"""
        serializer = QaRecordSerializer(self.qa, data={'answer': '新答案'}, partial=True)
        assert serializer.is_valid()
        qa = serializer.save()
        assert qa.answer == '新答案'
        assert qa.question == '问题'  # 未修改字段保持原值

    def test_tool_traces_with_agent_trace(self):
        """存在关联 AgentTrace 时 tool_traces 输出完整工具调用链"""
        from apps.agent.models import AgentTrace
        AgentTrace.objects.create(
            qa_record=self.qa, user=self.user, session=self.session,
            tool_name='knowledge_search', tool_args={'query': 'x'},
            tool_result='ok', result_ok=True, latency_ms=12)
        data = QaRecordSerializer(self.qa).data
        assert len(data['tool_traces']) == 1
        trace = data['tool_traces'][0]
        assert trace['tool_name'] == 'knowledge_search'
        assert trace['result_ok'] is True
        assert trace['latency_ms'] == 12

    def test_tool_traces_multiple_ordered(self):
        """多条 AgentTrace 按轮次顺序输出"""
        from apps.agent.models import AgentTrace
        AgentTrace.objects.create(
            qa_record=self.qa, user=self.user, session=self.session,
            tool_name='web_search', tool_args={}, result_ok=False, latency_ms=3)
        AgentTrace.objects.create(
            qa_record=self.qa, user=self.user, session=self.session,
            tool_name='calculator', tool_args={'expr': '1+1'}, result_ok=True, latency_ms=1)
        data = QaRecordSerializer(self.qa).data
        assert [t['tool_name'] for t in data['tool_traces']] == ['web_search', 'calculator']


@pytest.mark.django_db
class TestQaFeedbackSerializer:
    """QaFeedbackSerializer 测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/会话/QA 记录"""
        self.user = User.objects.create_user(
            username='ser-fb-user', email='ser-fb@test.com', password='testpass123')
        self.session = Session.objects.create(user=self.user, title='测试会话')
        self.qa = QaRecord.objects.create(
            session=self.session, user=self.user, question='问题', answer='答案')

    def test_create_valid(self):
        """合法数据创建成功：user/qa_record 通过 save 额外注入（序列化器不含这两个字段）"""
        serializer = QaFeedbackSerializer(
            data={'rating': 1, 'comment': '赞'})
        assert serializer.is_valid()
        fb = serializer.save(user=self.user, qa_record=self.qa)
        assert fb.rating == 1
        assert fb.qa_record == self.qa
        assert fb.user == self.user

    def test_qa_record_id_read_only(self):
        """qa_record_id 为只读字段（模型 FK 的 attname 被 DRF 映射为只读属性），输入值不进入 validated_data"""
        serializer = QaFeedbackSerializer(
            data={'qa_record_id': self.qa.id, 'rating': 1})
        assert serializer.is_valid()
        assert 'qa_record_id' not in serializer.validated_data

    def test_rating_type_validation(self):
        """rating 为整数类型字段（模型字段未挂 choices 枚举），非整数输入应校验失败"""
        for bad in ['abc', None]:
            serializer = QaFeedbackSerializer(data={'rating': bad})
            assert not serializer.is_valid()
            assert 'rating' in serializer.errors

    def test_rating_accepts_arbitrary_integer(self):
        """rating 未挂 choices，任意整数（如 2/-2）都能通过序列化器校验（枚举约束在模型层未启用）"""
        for value in [2, -2, 5]:
            serializer = QaFeedbackSerializer(data={'rating': value})
            assert serializer.is_valid()

    def test_status_read_only(self):
        """status 只读：创建时传入的 status 被忽略，保持默认 pending"""
        serializer = QaFeedbackSerializer(
            data={'rating': 1, 'status': 'resolved'})
        assert serializer.is_valid()
        fb = serializer.save(user=self.user, qa_record=self.qa)
        assert fb.status == 'pending'

    def test_partial_update(self):
        """部分更新仅修改传入字段，status 等只读字段不受影响"""
        fb = QaFeedback.objects.create(qa_record=self.qa, user=self.user, rating=0)
        serializer = QaFeedbackSerializer(fb, data={'rating': -1, 'comment': '不准确'}, partial=True)
        assert serializer.is_valid()
        updated = serializer.save()
        assert updated.rating == -1
        assert updated.comment == '不准确'
        assert updated.status == 'pending'  # 只读字段保持原值
