"""
工作流执行引擎（Engine / Executor）

职责：
- 按拓扑序执行节点 DAG（research 子Agent / tool 工具 / approval 人工确认 / finalize 汇总）
- 无依赖节点并行执行（ThreadPoolExecutor，参考大厂编排器的"按依赖批并行"策略）
- 失败自动重试；敏感工具（web_search/text2sql）隐式强制人工确认
- 终止策略：最大时长上限 + 审批阻塞（拒绝则降级/中止），防工作流失控
- 节点轨迹落 WorkflowNodeRun（"轨迹可查"），工具调用链汇总落 AgentTrace

对外入口：
- run_workflow_stream()：流式执行（SSE 事件协议，第一段：节点执行 → 审批阻塞或完成）
- resume_workflow()：人工确认后恢复执行（工单审批/驳回钩子调用，同步完成）
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List

from loguru import logger
from django.db import transaction
from django.utils import timezone

from apps.agent.models import AgentWorkflow, WorkflowNodeRun
from apps.agent.tools import ToolContext, get_default_registry
from apps.llm.factory import get_llm
from apps.llm.prompts.workflow import WORKFLOW_FINALIZE_SYSTEM, WORKFLOW_FINALIZE_USER_TEMPLATE

# 敏感工具：直接调用前必须人工确认（隐式审批节点，无需编排器显式声明）
SENSITIVE_TOOLS = {'web_search', 'text2sql'}
# 单节点最大尝试次数（含首次，失败自动重试）
MAX_ATTEMPTS = 2
# 无依赖节点并行度上限（并发线程数）
MAX_PARALLEL = 4

# approval 节点处理信号：阻塞整个工作流等待人工确认
BLOCKED = object()


def _needs_approval(node: dict) -> bool:
    """判断节点是否需要在执行前人工确认

    - approval 节点：显式人工确认
    - tool 节点且工具属于 SENSITIVE_TOOLS：隐式人工确认（联网/查库敏感操作）
    """
    if node.get('type') == 'approval':
        return True
    return node.get('type') == 'tool' and node.get('tool_name') in SENSITIVE_TOOLS


class WorkflowRunner:
    """工作流执行器 —— 持有工作流状态并按拓扑序推进

    一次实例对应一次"连续执行段"：流式第一段或审批恢复段。
    审批恢复时通过 restore_completed() 从 WorkflowNodeRun 恢复已完成节点。
    """

    def __init__(self, workflow: AgentWorkflow, user, session,
                 root_types: list = None, node_ids: list = None):
        self.workflow = workflow
        self.user = user
        self.session = session
        self.root_types = root_types
        self.node_ids = node_ids
        self.node_map = {n['id']: n for n in (workflow.definition or [])}
        self.node_runs = {r.node_id: r for r in workflow.node_runs.all()}
        self.ctx = ToolContext(user=user, session=session, root_types=root_types,
                               node_ids=node_ids, llm=get_llm())
        # node_id -> {'output', 'ok', 'meta', 'status'}
        self.completed: Dict[str, dict] = {}
        # 被跳过（上游失败/被拒/超时）的节点 id
        self.skipped: set = set()
        # 全局工具调用链（finalize 后统一落 AgentTrace）
        self.all_tool_traces: List[dict] = []
        self.degraded = False
        self.degraded_reasons: List[str] = []
        # 事件收集（流式模式由调用方转发）
        self.events: List[dict] = []
        self.blocked = False

    # ------------------------------------------------------------------
    # 事件与节点状态
    # ------------------------------------------------------------------
    def _emit(self, event: dict):
        self.events.append(event)

    def _mark_node(self, nid: str, status: str, *, emit: bool = True,
                   output: dict = None, error: str = '', latency: int = None,
                   attempt: int = None, input: dict = None,
                   ticket_id: int = None):
        """更新节点执行记录状态并收集轨迹事件"""
        run = self.node_runs[nid]
        run.status = status
        if input is not None:
            run.input = input
        if output is not None:
            run.output = output
        if error:
            run.error = error
        if latency is not None:
            run.latency_ms = latency
        if attempt:
            run.attempt = attempt
        if ticket_id is not None:
            run.ticket_id = ticket_id
        if status == 'running' and not run.started_at:
            run.started_at = timezone.now()
        if status in ('succeeded', 'failed', 'blocked', 'approved', 'rejected', 'skipped'):
            if not run.finished_at:
                run.finished_at = timezone.now()
        run.save()
        if emit:
            node = self.node_map.get(nid, {})
            if status == 'running':
                self._emit({
                    'type': 'workflow_node_start',
                    'node_id': nid, 'step_type': node.get('type'),
                    'name': node.get('name', nid),
                })
            elif status in ('succeeded', 'failed', 'approved', 'rejected', 'skipped'):
                self._emit({
                    'type': 'workflow_node_done',
                    'node_id': nid, 'step_type': node.get('type'),
                    'name': node.get('name', nid),
                    'status': status, 'latency_ms': run.latency_ms,
                })

    # ------------------------------------------------------------------
    # 单节点执行
    # ------------------------------------------------------------------
    def _execute_node(self, nid: str, emit: bool = True) -> dict:
        """执行单个节点（research/tool），失败自动重试

        返回 {'output', 'ok', 'meta'}；失败时 output 为错误摘要。
        """
        node = self.node_map[nid]
        ntype = node.get('type')
        # 节点输入快照（research=子问题 / tool=工具参数）
        node_input = {'question': node.get('question')} if ntype == 'research' else \
                     {'params': node.get('params')}
        self._mark_node(nid, 'running', emit=emit, input=node_input)
        t0 = time.time()
        last_err = ''
        result = {'output': '', 'ok': False, 'meta': {}}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if ntype == 'research':
                    result = self._run_research(node)
                elif ntype == 'tool':
                    result = self._run_tool(node)
                else:
                    result = {'output': '不支持的节点类型', 'ok': False, 'meta': {}}
                if result.get('ok'):
                    break
                last_err = (result.get('output') or '')[:300]
            except Exception as e:
                logger.exception(f'[WorkflowEngine] node {nid} attempt {attempt} error')
                last_err = f'{e.__class__.__name__}: {str(e)[:200]}'
                result = {'output': '', 'ok': False, 'meta': {}}
        latency_ms = int((time.time() - t0) * 1000)
        status = 'succeeded' if result.get('ok') else 'failed'
        self._mark_node(nid, status, emit=emit, output=result, error=last_err,
                        latency=latency_ms, attempt=attempt)
        # 收集工具调用链（供 AgentTrace 审计与引用汇总）
        for t in (result.get('meta') or {}).get('tool_traces', []):
            self.all_tool_traces.append(t)
        return result

    def _run_research(self, node: dict) -> dict:
        """research 节点：子 Agent 独立检索 + 推理（复用现有 ReAct 同步链路）"""
        from apps.agent.react import agent_ask
        result = agent_ask(self.user, node['question'], self.session,
                           root_types=self.root_types, node_ids=self.node_ids)
        answer = result.get('answer') or ''
        return {
            'output': answer,
            'ok': bool(answer) and answer != '[未生成内容]',
            'meta': {
                'citations': result.get('citations', []),
                'chunks': result.get('chunks', []),
                'tool_traces': result.get('tool_traces', []),
                'llm_stats': result.get('llm_stats', {}),
            },
        }

    def _run_tool(self, node: dict) -> dict:
        """tool 节点：直接调用工具注册表"""
        registry = get_default_registry()
        params = node.get('params') or {}
        ret = registry.execute(node['tool_name'], params, self.ctx)
        return {
            'output': ret.get('result', ''),
            'ok': ret.get('ok', False),
            'meta': {
                'tool_traces': [{
                    'round': 1, 'call_id': '', 'tool_name': node['tool_name'],
                    'tool_args': params, 'result': ret.get('result', ''),
                    'ok': ret.get('ok', False), 'meta': ret.get('meta', {}),
                    'latency_ms': ret.get('latency_ms', 0),
                }],
            },
            'latency_ms': ret.get('latency_ms', 0),
        }

    # ------------------------------------------------------------------
    # 人工确认节点处理
    # ------------------------------------------------------------------
    def _handle_approval(self, nid: str, node: dict) -> object:
        """处理需要人工确认的节点

        返回：
        - BLOCKED：整个工作流暂停，等待人工确认
        - 'approved'：确认已通过（approval 节点），视为完成
        - 'run_tool'：敏感工具已批准，可继续执行工具
        - 'rejected'：被拒绝，节点跳过并降级

        统一内嵌确认（HITL 不创建工单）：显式 approval 节点与敏感工具节点
        均在聊天界面内嵌展示确认/拒绝按钮，用户确认后直接调用 API 恢复工作流，
        避免为工作流确认产生工单、污染工单中心的待我审批。
        """
        run = self.node_runs[nid]
        if run.status == 'approved':
            # 审批已通过（resume 后）
            if node.get('type') == 'tool':
                return 'run_tool'
            # approval 节点：确认通过即视为完成
            self.completed[nid] = {
                'output': (run.output or {}).get('output') or '人工确认通过',
                'ok': True, 'meta': {}, 'status': 'approved',
            }
            return 'approved'
        if run.status == 'rejected':
            self.skipped.add(nid)
            self.degraded = True
            self.degraded_reasons.append(f'节点 {node.get("name") or nid} 被人工拒绝')
            return 'rejected'
        if run.status == 'blocked':
            # 已有待确认状态 → 维持等待，重新发送审批事件
            self.blocked = True
            self._emit_approval_event(nid, node, run)
            return BLOCKED

        # pending：首次遇到 → 统一标记 blocked，等待对话框内嵌确认（不创建工单）
        self._mark_node(nid, 'blocked', output={'output': '', 'meta': {}}, emit=True)
        self._emit_approval_event(nid, node, self.node_runs[nid])
        self.blocked = True
        return BLOCKED

    def _emit_approval_event(self, nid: str, node: dict, run):
        """发送审批事件（统一内嵌确认）

        前端在聊天界面内嵌渲染确认/拒绝按钮，用户确认后调用
        POST /api/v1/agent/workflows/{id}/approve/ 恢复工作流。
        """
        self._emit({
            'type': 'workflow_approval_required',
            'node_id': nid,
            'node_name': node.get('name', nid),
            'ticket_id': None,
            'reason': node.get('reason', ''),
            'approval_type': 'inline',
        })

    # ------------------------------------------------------------------
    # 拓扑序主循环
    # ------------------------------------------------------------------
    def restore_completed(self):
        """审批恢复段：从 WorkflowNodeRun 恢复已完成/跳过节点状态"""
        for r in self.node_runs.values():
            if r.status == 'succeeded':
                self.completed[r.node_id] = {
                    'output': (r.output or {}).get('output', ''),
                    'ok': True, 'meta': (r.output or {}).get('meta', {}),
                    'status': 'succeeded',
                }
                for t in ((r.output or {}).get('meta') or {}).get('tool_traces', []):
                    self.all_tool_traces.append(t)
            elif r.status == 'rejected':
                self.skipped.add(r.node_id)
                self.degraded = True
                self.degraded_reasons.append(f'节点 {r.node_name or r.node_id} 被人工拒绝')
            elif r.status == 'skipped':
                self.skipped.add(r.node_id)

    def execute(self) -> List[dict]:
        """按拓扑序执行剩余节点，返回本轮产生的事件列表

        每批取"依赖全部完成"的节点并行执行；人工确认节点会阻塞整个工作流。
        终止策略：最大时长上限（超时剩余节点全部跳过）+ 依赖不满足（跳过降级）。
        """
        self.events = []
        # 审批等待时间不计入执行时长：以工作流最近活跃时间为基准
        self.workflow.save(update_fields=['updated_at'])
        deadline = self.workflow.updated_at.timestamp() + (self.workflow.max_duration_sec or 300)
        node_ids = list(self.node_map.keys())
        # finalize 是汇总节点，由 run_finalize() 单独执行，不进入拓扑执行循环
        remaining = [nid for nid in node_ids
                     if nid not in self.completed and nid not in self.skipped
                     and self.node_map[nid].get('type') != 'finalize']

        while remaining and not self.blocked:
            # 1. 时长上限：超时中止，剩余节点跳过（防工作流失控）
            if time.time() > deadline:
                for nid in remaining:
                    self._mark_node(nid, 'skipped', emit=True)
                self.degraded = True
                self.degraded_reasons.append('工作流执行超时，未执行的节点已跳过')
                break

            # 2. 找当前可执行批（依赖必须全部成功；依赖被拒/失败/跳过的下游不执行，
            #    留待"依赖未满足"分支统一标记 skipped，防止被拒节点的下游继续跑）
            ready = [nid for nid in remaining
                     if all(dep in self.completed
                            for dep in self.node_map[nid].get('depends_on', []))]
            if not ready:
                # 依赖无法满足（上游失败/被拒/跳过后仍无就绪节点）→ 剩余全部跳过，降级
                for nid in remaining:
                    self._mark_node(nid, 'skipped', emit=True)
                self.degraded = True
                self.degraded_reasons.append('依赖未满足，后续节点已跳过')
                break

            # 3. 人工确认节点先处理（可能阻塞整个工作流）
            approval_ids = [nid for nid in ready if _needs_approval(self.node_map[nid])]
            normal_ids = [nid for nid in ready if nid not in approval_ids]
            for nid in approval_ids:
                sig = self._handle_approval(nid, self.node_map[nid])
                if sig is BLOCKED:
                    break
                if sig == 'run_tool':
                    # 敏感工具已批准：加入普通执行集
                    normal_ids.append(nid)
            if self.blocked:
                break

            # 4. 普通节点并行执行
            if normal_ids:
                with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(normal_ids))) as pool:
                    futures = {pool.submit(self._execute_node, nid): nid for nid in normal_ids}
                    for fut in as_completed(futures):
                        nid = futures[fut]
                        result = fut.result()
                        self.completed[nid] = result
                        if not result.get('ok'):
                            self.degraded = True
                            self.degraded_reasons.append(f'节点 {self.node_map[nid].get("name") or nid} 执行失败')

            # 5. 从剩余列表移除本轮已处理的节点
            for nid in ready:
                if nid in remaining:
                    remaining.remove(nid)

        return self.events

    # ------------------------------------------------------------------
    # 汇总（finalize）
    # ------------------------------------------------------------------
    def build_finalize_messages(self) -> List[dict]:
        """组装汇总节点输入：已完成节点的输出 + 降级说明"""
        lines = []
        for nid, res in self.completed.items():
            node = self.node_map.get(nid, {})
            out = (res.get('output') or '').strip()
            if out:
                lines.append(f'【{node.get("name") or nid}】\n{out}')
        if self.degraded_reasons:
            lines.append('【执行说明】' + '；'.join(self.degraded_reasons))
        node_outputs = '\n\n'.join(lines) or '（所有节点均未产出有效结果）'
        return [
            {'role': 'system', 'content': WORKFLOW_FINALIZE_SYSTEM},
            {'role': 'user', 'content': WORKFLOW_FINALIZE_USER_TEMPLATE.format(
                question=self.workflow.question, node_outputs=node_outputs)},
        ]

    def run_finalize(self) -> dict:
        """调用汇总 LLM 生成最终答案（非流式，供流式一次性下发/恢复段复用）"""
        llm = get_llm()
        resp = llm.chat(self.build_finalize_messages(), temperature=0.3, max_tokens=2048)
        return {
            'answer': resp.get('content', ''),
            'llm_stats': {
                'latency_llm_ms': resp.get('latency_ms', 0),
                'tokens_prompt': resp.get('prompt_tokens', 0),
                'tokens_completion': resp.get('completion_tokens', 0),
                'cost': resp.get('cost', 0),
                'llm_provider': getattr(llm, 'name', 'deepseek'),
                'llm_model': getattr(llm, 'model', 'deepseek-chat'),
            },
        }


# ============================================================================
# 对外入口
# ============================================================================

@transaction.atomic
def _create_workflow(user, session, question: str, plan: dict,
                     max_nodes: int, max_duration_sec: int,
                     root_type: str, turn_index: int) -> AgentWorkflow:
    """创建工作流实例 + 节点执行记录（definition 追加 finalize 汇总节点）

    finalize 节点依赖所有编排节点，保证汇总在全部节点（含降级跳过）之后执行。
    """
    nodes = plan.get('nodes') or []
    finalize = {
        'id': 'finalize', 'name': '汇总生成最终答案', 'type': 'finalize',
        'depends_on': [n['id'] for n in nodes],
    }
    definition = nodes + [finalize]
    workflow = AgentWorkflow.objects.create(
        user=user, session=session, question=question,
        definition=definition, status='running',
        max_nodes=max_nodes, max_duration_sec=max_duration_sec,
        started_at=timezone.now(),
        # 元信息快照（审批恢复段落库需要）
        result={'root_type': root_type, 'turn_index': turn_index},
    )
    runs = [WorkflowNodeRun(
        workflow=workflow, node_id=n['id'], node_name=n.get('name', ''),
        step_type=n.get('type', ''),
    ) for n in definition]
    WorkflowNodeRun.objects.bulk_create(runs)
    return workflow


def _persist_workflow_result(workflow: AgentWorkflow, answer: str,
                             citations: list, chunks: list,
                             tool_traces: list, llm_stats: dict,
                             degraded: bool) -> int:
    """工作流收尾：落 QaRecord + AgentTrace + 记忆 + 缓存 + 更新工作流终态

    Returns:
        QaRecord id（落库失败时返回 None，由调用方降级处理）
    """
    from apps.agent.executor import (
        _persist_qa, _should_update_cache, _update_cache, _collect_transform_route_trace,
    )
    from apps.agent.models import AgentTrace
    from apps.memory.manager import MemoryManager
    from apps.agent.react import _collect_citations

    user, session = workflow.user, workflow.session
    meta = workflow.result or {}
    root_type = meta.get('root_type') or 'company_doc'
    turn_index = meta.get('turn_index') or 1

    # 汇总引用（knowledge_search 工具调用链按文档合并）
    if tool_traces:
        citations, chunks = _collect_citations(tool_traces)

    # 内容审查：一次性全量审查（与 task_split 分支一致）
    filter_hit = None
    safe_answer = answer
    try:
        from apps.security.sensitive_filter import get_sensitive_filter
        sf = get_sensitive_filter()
        hits = sf.check(answer)
        block_hits = [h for h in hits if h.action == 'block']
        if block_hits:
            filter_hit = block_hits[0]
            safe_answer = ''
    except Exception:
        logger.exception('[WorkflowEngine] final answer filter failed, skip')

    total_ms = 0
    if workflow.started_at:
        total_ms = int((timezone.now() - workflow.started_at).total_seconds() * 1000)
    # 审批等待时间不计入耗时（从 started_at 算会被拉长，这里仅作展示值，可接受）

    qa = _persist_qa(
        user=user, session=session, question=workflow.question,
        answer=safe_answer, citations=citations,
        retrieval_hits=[c['chunk_id'] for c in chunks],
        retrieval_scores=[
            {'chunk_id': c['chunk_id'], 'rrf': c.get('rrf_score', 0),
             'rerank': c.get('rerank_score', 0)} for c in chunks
        ],
        stats={'latency_total_ms': total_ms},
        llm_stats=llm_stats, root_type=root_type, turn_index=turn_index,
        answer_type='agent', is_task_split=False,
        is_success=not filter_hit,
        is_filtered=filter_hit is not None,
        filter_reason=(f'output:{filter_hit.word}' if filter_hit else ''),
        route_trace=_collect_transform_route_trace(tool_traces) or None,
    )

    # 工具调用链审计（复用 AgentTrace，失败不影响主流程）
    if tool_traces:
        # 重新编号 round（并行节点产出的 trace 需要全局连续轮次）
        for i, t in enumerate(tool_traces, 1):
            t['round'] = i
        try:
            AgentTrace.batch_create_from_traces(qa, user, session, tool_traces)
        except Exception:
            logger.exception('[WorkflowEngine] AgentTrace batch_create failed')

    # 记忆 + 热点缓存（命中 block 不写，避免违规内容被缓存复用）
    MemoryManager().append_turn(session, workflow.question, safe_answer)
    if _should_update_cache('agent', filter_hit is not None) and not filter_hit:
        _update_cache(workflow.question, root_type, user, safe_answer, citations)

    # 工作流终态
    workflow.qa_record = qa
    workflow.finished_at = timezone.now()
    workflow.result = {
        'answer': safe_answer,
        'citations': citations,
        'llm_stats': llm_stats,
        'root_type': root_type,
        'turn_index': turn_index,
        'degraded_reasons': _degraded_reasons_of(workflow),
        'qa_id': qa.id,
        'filtered': filter_hit is not None,
    }
    workflow.status = 'degraded' if degraded and safe_answer else ('failed' if not safe_answer else 'succeeded')
    workflow.save()
    return qa.id


def _degraded_reasons_of(workflow: AgentWorkflow) -> list:
    """从节点轨迹汇总降级原因（供工作流详情页展示）"""
    reasons = []
    for r in workflow.node_runs.filter(status__in=('rejected', 'skipped', 'failed')):
        reasons.append(f'{r.node_name or r.node_id}: {r.status}')
    return reasons


def run_workflow_stream(user, session, question: str, plan: dict,
                        root_types: list = None, node_ids: list = None,
                        max_nodes: int = 10, max_duration_sec: int = 300):
    """流式执行工作流，yield SSE 事件

    事件协议：
        workflow_start          {workflow_id, nodes}
        workflow_node_start     {node_id, step_type, name}
        workflow_node_done      {node_id, step_type, name, status, latency_ms}
        workflow_approval_required {node_id, node_name, ticket_id, ticket_no, reason}
        first_token / delta     （finalize 汇总答案）
        content_filtered        （答案命中 block 时）
        done                    {message_id, workflow_id, status, citations, stats}

    审批阻塞时 done 无 message_id（workflow 停留 waiting_approval），
    用户审批后由 resume_workflow 同步完成剩余执行，前端轮询详情获取结果。
    """
    root_type = root_types[0] if root_types else 'company_doc'
    turn_index = (session.turn_count or 0) + 1
    workflow = _create_workflow(user, session, question, plan,
                                max_nodes, max_duration_sec, root_type, turn_index)

    yield {
        'type': 'workflow_start',
        'workflow_id': workflow.id,
        'nodes': workflow.definition,
        'status': 'running',
    }

    runner = WorkflowRunner(workflow, user, session, root_types, node_ids)
    events = runner.execute()
    yield from events

    # 审批阻塞：工作流停留 waiting_approval，结束第一段流式
    if runner.blocked:
        workflow.status = 'waiting_approval'
        workflow.save(update_fields=['status', 'updated_at'])
        yield {
            'type': 'done',
            'workflow_id': workflow.id,
            'message_id': None,
            'status': 'waiting_approval',
            'citations': [],
            'is_workflow': True,
            'stats': {'waiting_approval': True},
        }
        return

    # 全部节点执行完：finalize 汇总（非流式一次性输出，同 task_split 分支协议）
    final = runner.run_finalize()
    answer = final.get('answer', '')
    llm_stats = final.get('llm_stats', {})

    # 汇总答案一次性审查：命中 block 发拦截事件，否则下发脱敏后的 delta
    from apps.agent.executor import _check_full_text, _make_filtered_event
    safe_answer, filter_hit = _check_full_text(answer)
    yield {'type': 'first_token', 'ttfb_ms': 0}
    if filter_hit:
        yield _make_filtered_event(filter_hit)
        answer = ''
    else:
        yield {'type': 'delta', 'delta': safe_answer}

    # 落库 + 更新工作流终态（degraded 由节点失败/被拒/跳过触发）
    tool_traces = runner.all_tool_traces
    qa_id = _persist_workflow_result(
        workflow, answer, [], [], tool_traces, llm_stats, runner.degraded)

    yield {
        'type': 'done',
        'workflow_id': workflow.id,
        'message_id': qa_id,
        'status': workflow.status,
        'citations': workflow.result.get('citations', []),
        'is_workflow': True,
        'is_filtered': filter_hit is not None,
        'stats': {
            'total_ms': int((timezone.now() - workflow.started_at).total_seconds() * 1000)
            if workflow.started_at else 0,
            'is_workflow': True,
            'status': workflow.status,
        },
    }


def resume_workflow(workflow: AgentWorkflow, node_id: str, approved: bool):
    """人工确认后恢复工作流执行（由 WorkflowApprovalView 内嵌确认接口调用）

    - approved=True：节点置 approved（敏感工具则继续执行工具），工作流继续
    - approved=False：节点置 rejected，下游依赖跳过，基于已有结果降级汇总

    同步执行：审批请求线程内完成并落库，前端通过工作流详情 API 查看最终结果。
    """
    run = workflow.node_runs.filter(node_id=node_id).first()
    if not run:
        logger.warning(f'[WorkflowEngine] resume node {node_id} not found in workflow {workflow.id}')
        return

    if approved:
        run.status = 'approved'
        run.finished_at = timezone.now()
        run.save()
        logger.info(f'[WorkflowEngine] workflow {workflow.id} node {node_id} approved, resume')
    else:
        run.status = 'rejected'
        run.finished_at = timezone.now()
        run.save()
        logger.info(f'[WorkflowEngine] workflow {workflow.id} node {node_id} rejected, degrade')

    if workflow.status != 'waiting_approval':
        logger.warning(f'[WorkflowEngine] workflow {workflow.id} status={workflow.status}, skip resume')
        return

    workflow.status = 'running'
    workflow.save(update_fields=['status', 'updated_at'])

    user, session = workflow.user, workflow.session
    root_types = None
    runner = WorkflowRunner(workflow, user, session, root_types, None)
    runner.restore_completed()
    runner.execute()

    # 汇总 + 落库（同步完成）
    final = runner.run_finalize()
    answer = final.get('answer', '')
    # 恢复段答案同样过审（与流式段一致）
    from apps.agent.executor import _check_full_text
    safe_answer, filter_hit = _check_full_text(answer)
    qa_id = _persist_workflow_result(
        workflow, '' if filter_hit else safe_answer, [], [],
        runner.all_tool_traces, final.get('llm_stats', {}), runner.degraded)
    logger.info(f'[WorkflowEngine] workflow {workflow.id} finished status={workflow.status} qa={qa_id}')
