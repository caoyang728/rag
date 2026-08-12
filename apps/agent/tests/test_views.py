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


# ---------------------------------------------------------------------------
# AgentWorkflowDetailView / AgentWorkflowListView
# ---------------------------------------------------------------------------

class TestAgentWorkflowAPI:
    """GET /api/v1/agent/workflows/  工作流详情与列表（含节点执行轨迹）"""

    def _make_workflow(self, user, status='succeeded', question='测试问题'):
        """创建带一个节点轨迹的工作流记录（Detail 序列化需要节点数据）"""
        from apps.agent.models import AgentWorkflow, WorkflowNodeRun
        wf = AgentWorkflow.objects.create(
            user=user, question=question, status=status,
            definition=[{'id': 'research_1', 'step_type': 'research', 'name': '检索'}],
            result={'answer': '最终答案', 'citations': [{'doc_title': '文档'}],
                    'degraded_reasons': [], 'qa_id': 1, 'filtered': False},
            max_nodes=10, max_duration_sec=300,
            started_at='2026-01-01T00:00:00Z', finished_at='2026-01-01T00:01:00Z',
        )
        WorkflowNodeRun.objects.create(
            workflow=wf, node_id='research_1', node_name='检索', step_type='research',
            status='succeeded', attempt=1,
            input={'query': '测试问题'},
            output={'output': '检索结果片段', 'ok': True},
            latency_ms=120,
            started_at='2026-01-01T00:00:00Z', finished_at='2026-01-01T00:00:01Z',
        )
        return wf

    def test_detail_when_owner_then_returns_full_result(self, authed_client, test_user):
        """发起人本人可见完整轨迹（节点状态/输出/耗时）"""
        wf = self._make_workflow(test_user)
        resp = authed_client.get(f'/api/v1/agent/workflows/{wf.id}/')
        assert resp.status_code == 200
        data = resp.json()
        assert data['id'] == wf.id
        assert data['question'] == '测试问题'
        assert data['status'] == 'succeeded'
        assert data['status_display'] == '成功'
        assert data['result']['answer'] == '最终答案'
        assert data['result']['citations'] == [{'doc_title': '文档'}]
        assert len(data['nodes']) == 1
        node = data['nodes'][0]
        assert node['node_id'] == 'research_1'
        assert node['status'] == 'succeeded'
        assert node['output'] == '检索结果片段'
        assert node['ok'] is True
        assert node['latency_ms'] == 120
        assert node['ticket_id'] is None
        assert node['started_at'] is not None
        assert node['finished_at'] is not None
        assert data['started_at'] is not None
        assert data['finished_at'] is not None

    def test_detail_when_not_exists_then_returns_404(self, authed_client):
        resp = authed_client.get('/api/v1/agent/workflows/999999/')
        assert resp.status_code == 404
        assert '工作流不存在' in resp.json()['detail']

    def test_detail_when_other_user_then_returns_403(self, authed_client, test_user):
        """非发起人且非超管 → 403（轨迹含工具输入输出，属敏感信息）"""
        from apps.users.models import User
        other = User.objects.create_user(
            username='other', email='other@example.com', password='x')
        wf = self._make_workflow(other)
        resp = authed_client.get(f'/api/v1/agent/workflows/{wf.id}/')
        assert resp.status_code == 403
        assert '无权查看' in resp.json()['detail']

    def test_detail_when_super_admin_then_returns_200(self, authed_client, test_user):
        """超管可查看任何用户的工作流详情"""
        from unittest.mock import PropertyMock
        from apps.users.models import User
        other = User.objects.create_user(
            username='other2', email='other2@example.com', password='x')
        wf = self._make_workflow(other)
        # is_super_admin 为只读 property（基于角色关联判定），测试环境无角色种子，
        # 直接 mock property 模拟超管身份
        with patch.object(User, 'is_super_admin', new_callable=PropertyMock, return_value=True):
            resp = authed_client.get(f'/api/v1/agent/workflows/{wf.id}/')
        assert resp.status_code == 200
        assert resp.json()['id'] == wf.id

    def test_list_when_no_status_then_returns_all(self, authed_client, test_user):
        """不传 status → 返回当前用户全部工作流（按创建倒序）"""
        self._make_workflow(test_user, status='succeeded', question='问题A')
        self._make_workflow(test_user, status='waiting_approval', question='问题B')
        resp = authed_client.get('/api/v1/agent/workflows/')
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert {w['question'] for w in data} == {'问题A', '问题B'}

    def test_list_when_status_filter_then_only_matches(self, authed_client, test_user):
        """status 过滤只返回对应状态的工作流"""
        self._make_workflow(test_user, status='succeeded', question='问题A')
        self._make_workflow(test_user, status='waiting_approval', question='问题B')
        resp = authed_client.get('/api/v1/agent/workflows/?status=waiting_approval')
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]['question'] == '问题B'
        assert data[0]['status_display'] == '等待人工确认'

    def test_list_only_shows_own_workflows(self, authed_client, test_user):
        """列表只含当前用户自己的工作流，不越权看到他人记录"""
        from apps.users.models import User
        other = User.objects.create_user(
            username='other3', email='other3@example.com', password='x')
        self._make_workflow(other, status='succeeded', question='别人的')
        self._make_workflow(test_user, status='succeeded', question='我的')
        resp = authed_client.get('/api/v1/agent/workflows/')
        data = resp.json()
        assert len(data) == 1
        assert data[0]['question'] == '我的'
        assert data[0]['status_display'] == '成功'


# ---------------------------------------------------------------------------
# WorkflowApprovalView —— 工作流内嵌人工确认（敏感工具节点 HITL）
# ---------------------------------------------------------------------------

class TestWorkflowApprovalAPI:
    """POST /api/v1/agent/workflows/<id>/approve/  内嵌确认/拒绝

    与工单审批的区别：不创建工单，前端直接确认/拒绝并同步恢复工作流。
    覆盖：权限校验（发起人/超管）、状态校验（waiting_approval）、
    节点校验（存在且 blocked）、resume_workflow 成功/异常。
    """

    def _make_waiting_workflow(self, user, node_status='blocked'):
        """创建 waiting_approval 工作流 + 一个待确认节点"""
        from apps.agent.models import AgentWorkflow, WorkflowNodeRun
        wf = AgentWorkflow.objects.create(
            user=user, question='测试问题', status='waiting_approval',
            max_nodes=10, max_duration_sec=300,
        )
        WorkflowNodeRun.objects.create(
            workflow=wf, node_id='tool_1', node_name='发消息',
            step_type='tool', status=node_status,
        )
        return wf

    @patch('apps.agent.workflow.engine.resume_workflow')
    def test_approve_when_owner_approved_then_resumes(self, mock_resume, authed_client,
                                                      test_user):
        """发起人确认通过 → 以 approved=True 同步恢复工作流，返回确认结果"""
        wf = self._make_waiting_workflow(test_user)
        resp = authed_client.post(
            f'/api/v1/agent/workflows/{wf.id}/approve/',
            data='{"node_id": "tool_1", "approved": true}',
            content_type='application/json')
        assert resp.status_code == 200
        data = resp.json()
        assert data['workflow_id'] == wf.id
        assert data['node_id'] == 'tool_1'
        assert data['approved'] is True
        assert data['status'] == 'waiting_approval'
        mock_resume.assert_called_once_with(wf, node_id='tool_1', approved=True)

    @patch('apps.agent.workflow.engine.resume_workflow')
    def test_approve_when_rejected_then_resumes_false(self, mock_resume, authed_client,
                                                      test_user):
        """发起人驳回 → 以 approved=False 恢复（节点 rejected 降级）"""
        wf = self._make_waiting_workflow(test_user)
        resp = authed_client.post(
            f'/api/v1/agent/workflows/{wf.id}/approve/',
            data='{"node_id": "tool_1", "approved": false}',
            content_type='application/json')
        assert resp.status_code == 200
        assert resp.json()['approved'] is False
        mock_resume.assert_called_once_with(wf, node_id='tool_1', approved=False)

    def test_approve_when_workflow_not_exists_then_404(self, authed_client):
        """工作流不存在 → 404"""
        resp = authed_client.post(
            '/api/v1/agent/workflows/999999/approve/',
            {'node_id': 'tool_1'}, format='json')
        assert resp.status_code == 404
        assert '工作流不存在' in resp.json()['detail']

    def test_approve_when_other_user_then_403(self, authed_client, test_user):
        """非发起人且非超管 → 403（敏感工具节点的确认同样受权限约束）"""
        from apps.users.models import User
        other = User.objects.create_user(
            username='approve_other', email='approve_other@example.com', password='x')
        wf = self._make_waiting_workflow(other)
        resp = authed_client.post(
            f'/api/v1/agent/workflows/{wf.id}/approve/',
            {'node_id': 'tool_1'}, format='json')
        assert resp.status_code == 403
        assert '无权操作' in resp.json()['detail']

    @patch('apps.agent.workflow.engine.resume_workflow')
    def test_approve_when_not_waiting_approval_then_400(self, mock_resume, authed_client,
                                                        test_user):
        """工作流不在 waiting_approval（已超时/已恢复）→ 400 拒绝重复审批"""
        wf = self._make_waiting_workflow(test_user)
        wf.status = 'running'
        wf.save(update_fields=['status'])
        resp = authed_client.post(
            f'/api/v1/agent/workflows/{wf.id}/approve/',
            {'node_id': 'tool_1'}, format='json')
        assert resp.status_code == 400
        assert '无需审批' in resp.json()['detail']
        mock_resume.assert_not_called()

    @patch('apps.agent.workflow.engine.resume_workflow')
    def test_approve_when_node_id_missing_then_400(self, mock_resume, authed_client,
                                                   test_user):
        """未传 node_id → 400"""
        wf = self._make_waiting_workflow(test_user)
        resp = authed_client.post(
            f'/api/v1/agent/workflows/{wf.id}/approve/', {}, format='json')
        assert resp.status_code == 400
        assert 'node_id 必填' in resp.json()['detail']
        mock_resume.assert_not_called()

    @patch('apps.agent.workflow.engine.resume_workflow')
    def test_approve_when_node_not_exists_then_404(self, mock_resume, authed_client,
                                                   test_user):
        """node_id 不属于该工作流 → 404"""
        wf = self._make_waiting_workflow(test_user)
        resp = authed_client.post(
            f'/api/v1/agent/workflows/{wf.id}/approve/',
            {'node_id': 'no_such_node'}, format='json')
        assert resp.status_code == 404
        assert '节点不存在' in resp.json()['detail']
        mock_resume.assert_not_called()

    @patch('apps.agent.workflow.engine.resume_workflow')
    def test_approve_when_node_not_blocked_then_400(self, mock_resume, authed_client,
                                                    test_user):
        """节点已处理（非 blocked）→ 400，避免重复确认"""
        wf = self._make_waiting_workflow(test_user, node_status='succeeded')
        resp = authed_client.post(
            f'/api/v1/agent/workflows/{wf.id}/approve/',
            {'node_id': 'tool_1'}, format='json')
        assert resp.status_code == 400
        assert '无需审批' in resp.json()['detail']
        mock_resume.assert_not_called()

    @patch('apps.agent.workflow.engine.resume_workflow',
           side_effect=RuntimeError('engine down'))
    def test_approve_when_resume_raises_then_500(self, mock_resume, authed_client,
                                                 test_user):
        """恢复工作流抛异常 → 500（前端可重试）"""
        wf = self._make_waiting_workflow(test_user)
        resp = authed_client.post(
            f'/api/v1/agent/workflows/{wf.id}/approve/',
            {'node_id': 'tool_1'}, format='json')
        assert resp.status_code == 500
        assert '恢复工作流失败' in resp.json()['detail']
