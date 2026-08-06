"""
apps.agent.models 单元/集成测试

覆盖 AgentTrace 模型全部分支：
- __str__ 字符串表示
- batch_create_from_traces：空列表跳过 / 正常批量创建 / 结果截断 / bulk_create 异常容错
- 默认值与字段约束（tool_round/tool_args/result_ok/latency_ms 默认值）

需要 DB（QaRecord/Session 外键依赖），用 pytest + django_db 标记。
"""
import pytest

from apps.agent.models import AgentTrace
from apps.chat.models import QaRecord
from apps.memory.models import Session
from apps.users.models import User

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


# ----------------------------------------------------------------------------
# 辅助函数：构造用户 / 会话 / 问答记录（AgentTrace 的外键依赖）
# ----------------------------------------------------------------------------
def _make_user(username='agent-trace-user'):
    """创建测试用户"""
    return User.objects.create_user(
        username=username, email=f'{username}@test.com', password='testpass123')


def _make_session(user):
    """创建测试会话"""
    return Session.objects.create(user=user, title='trace 测试会话')


def _make_qa(session, user):
    """创建测试问答记录"""
    return QaRecord.objects.create(session=session, user=user, question='测试问题')


# ============================================================================
# AgentTrace __str__ 与默认值
# ============================================================================
class TestAgentTrace:
    """AgentTrace 基础字段与 __str__"""

    def test_str_when_normal_then_correct_format(self):
        """__str__ 应输出 [tool_name] round=N ok=布尔"""
        user = _make_user('str-user')
        session = _make_session(user)
        qa = _make_qa(session, user)
        trace = AgentTrace.objects.create(
            qa_record=qa, user=user, session=session,
            tool_name='calculator', tool_round=2, result_ok=False,
        )
        assert str(trace) == '[calculator] round=2 ok=False'

    def test_create_when_no_optional_fields_then_uses_defaults(self):
        """创建时未显式赋值的字段应取模型默认值"""
        user = _make_user('default-user')
        session = _make_session(user)
        qa = _make_qa(session, user)
        trace = AgentTrace.objects.create(
            qa_record=qa, tool_name='knowledge_search')
        assert trace.tool_round == 1
        assert trace.tool_args == {}
        assert trace.tool_result == ''
        assert trace.call_id == ''
        assert trace.result_ok is True
        assert trace.latency_ms == 0
        assert trace.user is None  # SET_NULL：未传时为 None
        assert trace.session is None


# ============================================================================
# batch_create_from_traces —— 批量持久化工具调用链
# ============================================================================
class TestAgentTraceBatchCreate:
    """batch_create_from_traces 全部分支"""

    def test_batch_create_when_empty_then_does_nothing(self):
        """空列表直接返回，不触发任何 DB 写入"""
        user = _make_user('empty-traces')
        session = _make_session(user)
        qa = _make_qa(session, user)
        # 不应抛错，也不应创建任何记录
        AgentTrace.batch_create_from_traces(qa, user, session, [])
        assert AgentTrace.objects.filter(qa_record=qa).count() == 0

    def test_batch_create_when_none_then_does_nothing(self):
        """None 被视为空（if not 判定），不应创建记录"""
        user = _make_user('none-traces')
        session = _make_session(user)
        qa = _make_qa(session, user)
        AgentTrace.batch_create_from_traces(qa, user, session, None)
        assert AgentTrace.objects.filter(qa_record=qa).count() == 0

    def test_batch_create_when_normal_then_creates_records(self):
        """正常批量创建：多条 trace 一次性写入，字段正确映射"""
        user = _make_user('batch-normal')
        session = _make_session(user)
        qa = _make_qa(session, user)
        traces = [
            {
                'round': 1, 'call_id': 'call_1', 'tool_name': 'knowledge_search',
                'tool_args': {'query': '问 1'}, 'result': '结果 1',
                'ok': True, 'latency_ms': 120,
            },
            {
                'round': 2, 'call_id': 'call_2', 'tool_name': 'calculator',
                'tool_args': {'expression': '1+1'}, 'result': '2',
                'ok': False, 'latency_ms': 5,
            },
        ]
        AgentTrace.batch_create_from_traces(qa, user, session, traces)

        records = list(AgentTrace.objects.filter(qa_record=qa).order_by('tool_round'))
        assert len(records) == 2

        r1 = records[0]
        assert r1.tool_round == 1
        assert r1.call_id == 'call_1'
        assert r1.tool_name == 'knowledge_search'
        assert r1.tool_args == {'query': '问 1'}
        assert r1.tool_result == '结果 1'
        assert r1.result_ok is True
        assert r1.latency_ms == 120
        assert r1.user_id == user.id
        assert r1.session_id == session.id

        r2 = records[1]
        assert r2.tool_round == 2
        assert r2.result_ok is False
        assert r2.latency_ms == 5

    def test_batch_create_when_long_result_then_truncated(self):
        """工具结果超过 5000 字符时截断，并附加截断提示"""
        user = _make_user('trunc-user')
        session = _make_session(user)
        qa = _make_qa(session, user)
        long_result = 'x' * 6000
        traces = [{
            'round': 1, 'tool_name': 'web_search',
            'result': long_result, 'ok': True, 'latency_ms': 200,
        }]
        AgentTrace.batch_create_from_traces(qa, user, session, traces)

        record = AgentTrace.objects.get(qa_record=qa)
        # 截断后长度 = 5000 + 截断提示文本长度
        assert len(record.tool_result) < len(long_result)
        assert record.tool_result.startswith('x' * 5000)
        assert '结果已截断' in record.tool_result

    def test_batch_create_when_missing_fields_then_uses_defaults(self):
        """trace 缺失字段时使用默认值：round=1, ok=True, latency_ms=0 等"""
        user = _make_user('missing-fields')
        session = _make_session(user)
        qa = _make_qa(session, user)
        # 仅给 tool_name，其他字段缺失
        traces = [{'tool_name': 'wiki_search'}]
        AgentTrace.batch_create_from_traces(qa, user, session, traces)

        record = AgentTrace.objects.get(qa_record=qa)
        assert record.tool_round == 1
        assert record.tool_name == 'wiki_search'
        assert record.tool_args == {}
        assert record.tool_result == ''
        assert record.call_id == ''
        assert record.result_ok is True
        assert record.latency_ms == 0

    def test_batch_create_when_bulk_create_fails_then_does_not_raise(self, monkeypatch):
        """bulk_create 抛异常时被捕获，不阻断主流程（仅记录日志）"""
        user = _make_user('fail-bulk')
        session = _make_session(user)
        qa = _make_qa(session, user)
        traces = [{'round': 1, 'tool_name': 'calculator', 'result': 'ok'}]

        # 模拟 bulk_create 抛异常，验证不影响主流程
        def _raise(*args, **kwargs):
            raise RuntimeError('db connection lost')

        monkeypatch.setattr(AgentTrace.objects, 'bulk_create', _raise)

        # 不应抛异常
        AgentTrace.batch_create_from_traces(qa, user, session, traces)
        # 异常被吞掉，记录未写入
        assert AgentTrace.objects.filter(qa_record=qa).count() == 0
