"""
工作流执行引擎（engine）测试

覆盖：
- 无审批工作流：拓扑序执行全部节点 + finalize 汇总 + 落库收尾
- approval 节点阻塞：等待人工确认（waiting_approval，done 无 message_id）
- 敏感工具（web_search/text2sql）隐式强制人工确认
- resume_workflow：审批通过继续 / 审批拒绝降级
- 节点失败自动重试 + 降级汇总

需要 DB（AgentWorkflow/WorkflowNodeRun 轨迹落库），LLM / 子 Agent / 工具 /
落库辅助函数全部 mock，不依赖外部服务。
"""
import pytest
from unittest.mock import patch, MagicMock

from apps.agent.workflow import engine

pytestmark = pytest.mark.integration


def _make_llm(answer='最终汇总答案'):
    """构造 mock LLM（finalize 汇总用）"""
    mock_llm = MagicMock()
    mock_llm.chat.return_value = {
        'content': answer, 'latency_ms': 10,
        'prompt_tokens': 20, 'completion_tokens': 10, 'cost': 0.001,
    }
    mock_llm.name = 'test-llm'
    mock_llm.model = 'test-model'
    return mock_llm


def _agent_ask_side_effect(*, questions):
    """research 节点子 Agent mock：按子问题文本返回对应答案"""
    def side_effect(user, question, session, root_types=None, node_ids=None, sources=None):
        return {
            'answer': questions.get(question, '默认答案'),
            'citations': [], 'chunks': [], 'tool_traces': [],
            'llm_stats': {'latency_llm_ms': 1},
        }
    return side_effect


from concurrent.futures import Future as _RealFuture


class _SyncFuture(_RealFuture):
    """同步 Future：submit 时立即执行并标记完成

    继承真实 concurrent.futures.Future 以兼容 as_completed（其依赖
    _condition/_state/_waiters 等内部属性）。submit 时即执行节点函数，
    as_completed 立即返回，result() 直接取缓存结果。
    """
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        try:
            result = fn(*args, **kwargs)
            self.set_result(result)
        except BaseException as e:  # noqa: BLE001 - 与 Future 语义一致，异常在 result() 时抛出
            self.set_exception(e)


class _SyncExecutor:
    """同步执行器：submit 立即执行，不启动子线程

    pytest-django 的 db fixture 用单事务包裹主线程，而引擎默认用
    ThreadPoolExecutor 子线程写库（子线程走连接池独立连接）→ 子线程写
    WorkflowNodeRun 时等待主线程未提交事务的行锁，主线程又等子线程
    future 结果，必然死锁。测试替换为同步执行器后，所有节点都在主线程
    事务内顺序执行，测试结束随事务回滚，无需 truncate。
    """
    def __init__(self, *args, **kwargs):
        pass

    def submit(self, fn, *args, **kwargs):
        return _SyncFuture(fn, *args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def workflow_env(test_user, db):
    """工作流测试环境：用户 + 会话 + 落库辅助函数全部 mock

    关键：将 engine.ThreadPoolExecutor 替换为同步执行器（_SyncExecutor），
    使所有节点在主线程事务内顺序执行。若不替换，引擎子线程通过连接池的
    独立连接写 DB，会与 pytest-django 单事务包裹的主线程互相等待行锁而死锁；
    并行行为由生产环境线程池保证，测试只需验证拓扑序/审批/降级逻辑。
    """
    from apps.memory.models import Session
    session = Session.objects.create(user=test_user, title='工作流测试会话')

    def _fake_persist_qa(**kwargs):
        """落库辅助函数 mock：返回真实 QaRecord（workflow.qa_record 外键需要实例）"""
        from apps.chat.models import QaRecord
        return QaRecord.objects.create(
            session=kwargs['session'], user=kwargs['user'],
            question=kwargs.get('question') or 'wf',
            answer=kwargs.get('answer') or '',
            root_type=kwargs.get('root_type') or 'company_doc',
            turn_index=kwargs.get('turn_index') or 1,
        )

    with patch.object(engine, 'ThreadPoolExecutor', _SyncExecutor), \
         patch.object(engine, 'MAX_PARALLEL', 1), \
         patch.object(engine, 'get_llm', return_value=_make_llm()), \
         patch('apps.agent.react.agent_ask'), \
         patch('apps.agent.react._collect_citations', return_value=([], [])), \
         patch('apps.agent.executor._persist_qa', side_effect=_fake_persist_qa), \
         patch('apps.agent.executor._should_update_cache', return_value=True), \
         patch('apps.agent.executor._update_cache'), \
         patch('apps.agent.executor._collect_transform_route_trace', return_value=None), \
         patch('apps.agent.executor._check_full_text', return_value=('答案', None)), \
         patch('apps.agent.executor._make_filtered_event'), \
         patch('apps.memory.manager.MemoryManager'), \
         patch('apps.agent.models.AgentTrace.batch_create_from_traces'):
        yield test_user, session


class TestRunWorkflowStream:
    """run_workflow_stream：流式执行工作流"""

    def test_run_workflow_when_no_approval_then_all_nodes_executed(self, workflow_env):
        """无审批节点：research 并行执行 + finalize 汇总，done 携带 message_id 与 succeeded 状态"""
        user, session = workflow_env
        plan = {'nodes': [
            {'id': 'r1', 'name': '研究A', 'type': 'research', 'question': '子问题A', 'depends_on': []},
            {'id': 'r2', 'name': '研究B', 'type': 'research', 'question': '子问题B', 'depends_on': []},
        ]}

        from apps.agent.react import agent_ask
        agent_ask.side_effect = _agent_ask_side_effect(
            questions={'子问题A': '结果A', '子问题B': '结果B'})

        events = list(engine.run_workflow_stream(user, session, '总问题', plan))
        types = [e['type'] for e in events]

        assert 'workflow_start' in types
        assert types.count('workflow_node_start') == 3   # r1 + r2 + finalize（汇总节点补发轨迹事件）
        assert types.count('workflow_node_done') == 3
        assert 'first_token' in types
        assert 'delta' in types

        done = events[-1]
        assert done['type'] == 'done'
        assert done['message_id'] is not None
        assert done['status'] == 'succeeded'
        assert done['is_workflow'] is True

        # 轨迹落库：r1/r2/finalize 全部 succeeded（汇总节点状态同步落库）
        from apps.agent.models import AgentWorkflow
        wf = AgentWorkflow.objects.get(id=done['workflow_id'])
        assert wf.status == 'succeeded'
        assert wf.node_runs.count() == 3
        assert wf.node_runs.filter(status='succeeded').count() == 3
        finalize = wf.node_runs.get(node_id='finalize')
        assert finalize.status == 'succeeded'
        assert finalize.latency_ms is not None

    def test_run_workflow_when_approval_node_then_blocked(self, workflow_env):
        """approval 节点：统一内嵌确认（不创建工单），工作流停留 waiting_approval，done 无 message_id"""
        user, session = workflow_env
        plan = {'nodes': [
            {'id': 'r1', 'name': '研究', 'type': 'research', 'question': '子问题', 'depends_on': []},
            {'id': 'ap1', 'name': '人工确认', 'type': 'approval',
             'reason': '需要人工确认后才能继续', 'depends_on': ['r1']},
        ]}

        from apps.agent.react import agent_ask
        agent_ask.return_value = {'answer': '结果', 'citations': [], 'chunks': [],
                                  'tool_traces': [], 'llm_stats': {}}

        events = list(engine.run_workflow_stream(user, session, '总问题', plan))
        appr_ev = [e for e in events if e['type'] == 'workflow_approval_required']
        assert len(appr_ev) == 1
        # 统一内嵌确认：不产生工单，事件不含 ticket_id
        assert appr_ev[0]['approval_type'] == 'inline'
        assert appr_ev[0]['ticket_id'] is None
        assert appr_ev[0]['node_id'] == 'ap1'

        done = events[-1]
        assert done['status'] == 'waiting_approval'
        assert done['message_id'] is None

        from apps.agent.models import AgentWorkflow
        wf = AgentWorkflow.objects.get(id=done['workflow_id'])
        assert wf.status == 'waiting_approval'
        node = wf.node_runs.get(node_id='ap1')
        assert node.status == 'blocked'
        assert node.ticket_id is None

    def test_run_workflow_when_sensitive_tool_then_blocked(self, workflow_env):
        """敏感工具（web_search）不显式声明审批也须强制人工确认"""
        user, session = workflow_env
        plan = {'nodes': [
            {'id': 'w1', 'name': '联网搜索', 'type': 'tool',
             'tool_name': 'web_search', 'params': {'query': 'xx'}, 'depends_on': []},
        ]}

        events = list(engine.run_workflow_stream(user, session, '联网问题', plan))
        appr_ev = [e for e in events if e['type'] == 'workflow_approval_required']
        assert len(appr_ev) == 1
        assert appr_ev[0]['node_id'] == 'w1'
        assert appr_ev[0]['approval_type'] == 'inline'

        done = events[-1]
        assert done['status'] == 'waiting_approval'

    def test_run_workflow_when_node_fails_then_retry_and_degrade(self, workflow_env):
        """research 节点最终失败：自动重试后仍失败 → 工作流降级汇总（degraded）"""
        user, session = workflow_env
        plan = {'nodes': [
            {'id': 'r1', 'name': '会失败的研究', 'type': 'research',
             'question': '子问题', 'depends_on': []},
        ]}

        from apps.agent.react import agent_ask
        agent_ask.return_value = {'answer': '', 'citations': [], 'chunks': [],
                                  'tool_traces': [], 'llm_stats': {}}

        events = list(engine.run_workflow_stream(user, session, '总问题', plan))
        done = events[-1]
        # 答案为空 → failed（降级仍能收尾，workflow 终态不为 waiting_approval）
        assert done['type'] == 'done'
        assert done['status'] in ('failed', 'degraded')

        from apps.agent.models import AgentWorkflow
        wf = AgentWorkflow.objects.get(id=done['workflow_id'])
        node = wf.node_runs.get(node_id='r1')
        assert node.status == 'failed'
        assert node.attempt == 2  # 失败后自动重试过一次


class TestResumeWorkflow:
    """resume_workflow：人工确认后恢复工作流"""

    def _make_waiting_workflow(self, user, session, nodes, blocked_node_id):
        """构造 waiting_approval 状态的工作流（含 blocked 审批节点）"""
        wf = engine._create_workflow(user, session, '总问题',
                                     {'nodes': nodes}, 10, 300, 'company_doc', 1)
        wf.status = 'waiting_approval'
        wf.save(update_fields=['status'])
        run = wf.node_runs.get(node_id=blocked_node_id)
        run.status = 'blocked'
        run.ticket_id = 999
        run.save()
        return wf

    def test_resume_when_approved_then_continues_and_succeeds(self, workflow_env):
        """审批通过：节点置 approved，工作流继续执行后续节点并成功收尾"""
        user, session = workflow_env
        nodes = [
            {'id': 'r1', 'name': '研究', 'type': 'research', 'question': '子问题', 'depends_on': []},
            {'id': 'ap1', 'name': '人工确认', 'type': 'approval',
             'reason': '确认', 'depends_on': ['r1']},
            {'id': 'r2', 'name': '后续研究', 'type': 'research', 'question': '后续问题', 'depends_on': ['ap1']},
        ]
        wf = self._make_waiting_workflow(user, session, nodes, 'ap1')

        from apps.agent.react import agent_ask
        agent_ask.side_effect = _agent_ask_side_effect(
            questions={'子问题': '前序结果', '后续问题': '后续结果'})

        engine.resume_workflow(wf, 'ap1', approved=True)

        wf.refresh_from_db()
        assert wf.status == 'succeeded'
        assert wf.node_runs.get(node_id='ap1').status == 'approved'
        assert wf.node_runs.get(node_id='r2').status == 'succeeded'
        assert wf.result.get('answer')

    def test_resume_when_rejected_then_degrades(self, workflow_env):
        """审批拒绝：节点置 rejected，下游跳过，基于已有结果降级汇总"""
        user, session = workflow_env
        nodes = [
            {'id': 'r1', 'name': '研究', 'type': 'research', 'question': '子问题', 'depends_on': []},
            {'id': 'ap1', 'name': '人工确认', 'type': 'approval',
             'reason': '确认', 'depends_on': ['r1']},
            {'id': 'r2', 'name': '后续研究', 'type': 'research', 'question': '后续问题', 'depends_on': ['ap1']},
        ]
        wf = self._make_waiting_workflow(user, session, nodes, 'ap1')

        from apps.agent.react import agent_ask
        agent_ask.return_value = {'answer': '结果', 'citations': [], 'chunks': [],
                                  'tool_traces': [], 'llm_stats': {}}

        engine.resume_workflow(wf, 'ap1', approved=False)

        wf.refresh_from_db()
        assert wf.status == 'degraded'
        assert wf.node_runs.get(node_id='ap1').status == 'rejected'
        # 下游节点因依赖被拒而跳过
        assert wf.node_runs.get(node_id='r2').status == 'skipped'


class TestWorkflowRunnerTopo:
    """WorkflowRunner 拓扑序行为（复用真实 DB 轨迹）"""

    def test_runner_respects_dependency_order(self, workflow_env):
        """依赖链 r1 → r2：r2 必须在 r1 成功后才执行（串行顺序可断言）"""
        user, session = workflow_env
        nodes = [
            {'id': 'r1', 'name': 'A', 'type': 'research', 'question': '问题A', 'depends_on': []},
            {'id': 'r2', 'name': 'B', 'type': 'research', 'question': '问题B', 'depends_on': ['r1']},
        ]
        from apps.agent.react import agent_ask
        order = []
        def side_effect(user, question, session, root_types=None, node_ids=None, sources=None):
            order.append(question)
            return {'answer': '答案:' + question, 'citations': [], 'chunks': [],
                    'tool_traces': [], 'llm_stats': {}}
        agent_ask.side_effect = side_effect

        from apps.agent.models import AgentWorkflow
        wf = engine._create_workflow(user, session, '总问题',
                                     {'nodes': nodes}, 10, 300, 'company_doc', 1)
        runner = engine.WorkflowRunner(wf, user, session, None, None)
        runner.execute()

        assert order == ['问题A', '问题B']
        assert set(runner.completed.keys()) == {'r1', 'r2'}
