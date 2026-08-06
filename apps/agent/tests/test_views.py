"""
agent.views 接口测试

覆盖 AgentTaskPlanView / AgentTaskRunView 的全部分支：
- 参数校验（空 question → 400）
- session 查找与创建（session_id 不存在 → 404 / 未传 → 新建）
- ask_stream 流式事件消费（delta 拼接 / content_filtered 拦截 / error 事件）
- executor 调用异常 → 500

输入/输出审查由 executor.ask_stream 内部处理，view 层不再做审查，故不测审查分支。
"""
import pytest
from unittest.mock import patch

pytestmark = pytest.mark.integration


@pytest.fixture
def authed_client(client, test_user):
    """已认证的 DRF 测试客户端（复用 conftest 的 test_user）"""
    client.force_login(test_user)
    return client


def _stream(*events):
    """构造 SSE 事件迭代器（模拟 ask_stream 生成器输出）"""
    return iter(events)


# ---------------------------------------------------------------------------
# AgentTaskPlanView
# ---------------------------------------------------------------------------

class TestAgentTaskPlanAPI:
    """POST /api/v1/agent/task/plan/  任务拆分预览"""

    def test_plan_when_question_empty_then_returns_400(self, authed_client):
        """空 question 返回 400"""
        resp = authed_client.post('/api/v1/agent/task/plan/', {'question': ''}, format='json')
        assert resp.status_code == 400
        assert 'question 必填' in resp.json()['detail']

    def test_plan_when_question_missing_then_returns_400(self, authed_client):
        """未传 question 返回 400"""
        resp = authed_client.post('/api/v1/agent/task/plan/', {}, format='json')
        assert resp.status_code == 400

    @patch('apps.agent.task_splitter.maybe_split')
    def test_plan_when_llm_succeeds_then_returns_plan(self, mock_split, authed_client):
        """maybe_split 正常返回时透传拆分结果"""
        mock_split.return_value = {'need_split': True, 'sub_tasks': [{'index': 1}]}
        resp = authed_client.post(
            '/api/v1/agent/task/plan/', {'question': '复杂问题'}, format='json')
        assert resp.status_code == 200
        data = resp.json()
        assert data['need_split'] is True
        mock_split.assert_called_once_with('复杂问题')

    @patch('apps.agent.task_splitter.maybe_split', side_effect=RuntimeError('llm down'))
    def test_plan_when_maybe_split_raises_then_returns_500(self, mock_split, authed_client):
        """maybe_split 抛异常时返回 500"""
        resp = authed_client.post(
            '/api/v1/agent/task/plan/', {'question': '问题'}, format='json')
        assert resp.status_code == 500
        assert '拆分失败' in resp.json()['detail']


# ---------------------------------------------------------------------------
# AgentTaskRunView
# ---------------------------------------------------------------------------

class TestAgentTaskRunAPI:
    """POST /api/v1/agent/task/run/  任务拆分并执行（走 ask_stream 流式）"""

    def test_run_when_question_empty_then_returns_400(self, authed_client):
        """空 question 返回 400"""
        resp = authed_client.post('/api/v1/agent/task/run/', {'question': ''}, format='json')
        assert resp.status_code == 400
        assert 'question 必填' in resp.json()['detail']

    @patch('apps.agent.executor.ask_stream')
    def test_run_when_session_not_found_then_returns_404(self, mock_stream, authed_client):
        """session_id 指向不存在的会话 → 404"""
        resp = authed_client.post(
            '/api/v1/agent/task/run/',
            {'question': '问题', 'session_id': 999999},
            format='json')
        assert resp.status_code == 404
        assert 'session 不存在' in resp.json()['detail']
        mock_stream.assert_not_called()

    @patch('apps.agent.executor.ask_stream')
    def test_run_when_no_session_id_then_creates_session(self, mock_stream, authed_client,
                                                         test_user):
        """未传 session_id 时自动创建新会话"""
        mock_stream.return_value = _stream(
            {'type': 'start', 'session_id': 0, 'citations': [], 'is_hit_cache': False},
            {'type': 'first_token', 'ttfb_ms': 5},
            {'type': 'delta', 'delta': '答案'},
            {'type': 'done', 'message_id': 1, 'session_id': 0,
             'citations': [], 'stats': {}, 'is_filtered': False},
        )
        resp = authed_client.post(
            '/api/v1/agent/task/run/', {'question': '新问题'}, format='json')
        assert resp.status_code == 200
        data = resp.json()
        assert 'session_id' in data
        from apps.memory.models import Session
        session = Session.objects.get(id=data['session_id'])
        assert session.user_id == test_user.id
        assert session.title == '新问题'

    @patch('apps.agent.executor.ask_stream', side_effect=RuntimeError('executor crashed'))
    def test_run_when_executor_raises_then_returns_500(self, mock_stream, authed_client):
        """ask_stream 抛异常 → 500"""
        resp = authed_client.post(
            '/api/v1/agent/task/run/', {'question': '问题'}, format='json')
        assert resp.status_code == 500
        assert '内部错误' in resp.json()['detail']

    @patch('apps.agent.executor.ask_stream')
    def test_run_when_success_then_returns_full_result(self, mock_stream, authed_client):
        """正常成功路径：消费 delta 事件拼接 answer，done 事件提取元数据"""
        mock_stream.return_value = _stream(
            {'type': 'start', 'session_id': 0, 'citations': [], 'is_hit_cache': False},
            {'type': 'first_token', 'ttfb_ms': 10},
            {'type': 'delta', 'delta': '综合'},
            {'type': 'delta', 'delta': '答案'},
            {'type': 'done', 'message_id': 42, 'session_id': 0,
             'citations': [{'doc_title': '文档A'}],
             'stats': {'total_ms': 100}, 'is_filtered': False},
        )
        resp = authed_client.post(
            '/api/v1/agent/task/run/', {'question': '复杂问题'}, format='json')
        assert resp.status_code == 200
        data = resp.json()
        assert data['answer'] == '综合答案'
        assert data['message_id'] == 42
        assert data['citations'] == [{'doc_title': '文档A'}]
        assert data['stats'] == {'total_ms': 100}
        assert 'is_filtered' not in data

    @patch('apps.agent.executor.ask_stream')
    def test_run_when_content_filtered_then_returns_filtered(self, mock_stream, authed_client):
        """流中收到 content_filtered 事件 → 返回拦截响应"""
        mock_stream.return_value = _stream(
            {'type': 'start', 'session_id': 0, 'citations': [], 'is_hit_cache': False},
            {'type': 'first_token', 'ttfb_ms': 5},
            {'type': 'content_filtered', 'reason': '检测到违规内容，已拦截',
             'category': 'porn'},
            {'type': 'done', 'message_id': 7, 'session_id': 0,
             'citations': [], 'stats': {}, 'is_filtered': True},
        )
        resp = authed_client.post(
            '/api/v1/agent/task/run/', {'question': '违禁问题'}, format='json')
        assert resp.status_code == 200
        data = resp.json()
        assert data['is_filtered'] is True
        assert data['answer'] == ''
        assert data['category'] == 'porn'
        assert data['message_id'] == 7

    @patch('apps.agent.executor.ask_stream')
    def test_run_when_error_event_then_returns_500(self, mock_stream, authed_client):
        """流中收到 error 事件 → 返回 500"""
        mock_stream.return_value = _stream(
            {'type': 'error', 'detail': '任务拆分执行失败: LLM 超时'},
        )
        resp = authed_client.post(
            '/api/v1/agent/task/run/', {'question': '问题'}, format='json')
        assert resp.status_code == 500
        assert '任务拆分执行失败' in resp.json()['detail']

    @patch('apps.agent.executor.ask_stream')
    def test_run_when_masked_output_then_returns_masked_answer(self, mock_stream,
                                                               authed_client):
        """mask 命中：delta 已是脱敏后的安全文本，正常拼接返回"""
        mock_stream.return_value = _stream(
            {'type': 'start', 'session_id': 0, 'citations': [], 'is_hit_cache': False},
            {'type': 'first_token', 'ttfb_ms': 8},
            {'type': 'delta', 'delta': '手机号 ***'},
            {'type': 'done', 'message_id': 5, 'session_id': 0,
             'citations': [], 'stats': {}, 'is_filtered': False},
        )
        resp = authed_client.post(
            '/api/v1/agent/task/run/', {'question': '问题'}, format='json')
        assert resp.status_code == 200
        assert resp.json()['answer'] == '手机号 ***'

    @patch('apps.agent.executor.ask_stream')
    def test_run_when_task_split_then_stats_includes_flag(self, mock_stream, authed_client):
        """任务拆分路径：done 事件 stats 中 is_task_split=True"""
        mock_stream.return_value = _stream(
            {'type': 'start', 'session_id': 0, 'citations': [], 'is_hit_cache': False},
            {'type': 'first_token', 'ttfb_ms': 50},
            {'type': 'delta', 'delta': '合并答案'},
            {'type': 'done', 'message_id': 10, 'session_id': 0,
             'citations': [],
             'stats': {'total_ms': 200, 'ttfb_ms': 50, 'is_task_split': True},
             'is_filtered': False},
        )
        resp = authed_client.post(
            '/api/v1/agent/task/run/', {'question': '复杂问题'}, format='json')
        assert resp.status_code == 200
        data = resp.json()
        assert data['answer'] == '合并答案'
        assert data['stats']['is_task_split'] is True
