"""
工作流编排器（Planner / Orchestrator）

- maybe_plan()：调用 LLM 判断问题是否需要多 Agent 工作流，并产出节点 DAG
- validate_dag()：校验节点 DAG 合法性（类型/工具名/depends_on 引用/循环依赖/节点数上限）
- 简单问题返回 need_workflow=false，保持与现有 ReAct 单轮链路兼容

节点 DAG 约定（definition 数组元素）：
    research：子 Agent 独立检索+推理，字段 {id, name, type='research', question, depends_on}
    tool    ：直接调用注册表工具，字段 {id, name, type='tool', tool_name, params, depends_on}
    approval：人工确认节点，字段 {id, name, type='approval', reason, depends_on}
引擎会追加 finalize 汇总节点；web_search/text2sql 敏感工具由引擎强制加审批，
不依赖编排器显式声明（编排器 prompt 已约定不加）。
"""
import json
from typing import Any, Dict, List

from loguru import logger

from apps.llm.factory import get_llm
from apps.llm.prompts.workflow import WORKFLOW_PLAN_SYSTEM, WORKFLOW_PLAN_USER_TEMPLATE
from apps.agent.tools import get_default_registry


# 合法节点类型
VALID_NODE_TYPES = {'research', 'tool', 'approval'}

# 内部知识检索统一入口映射：wiki_search/graph_search 已并入 knowledge_search
# （knowledge_search 内部按 Wiki → 图谱 → 文档 三层固定顺序检索）。
# 编排器若偶发生成独立的 wiki/graph 工具节点，直接改写为统一入口，
# 防止 LLM 乱序调用破坏固定检索顺序（提示词约束 + 此处归一化双保险）。
_DOC_UNIFIED_TOOLS = {
    'wiki_search': 'knowledge_search',
    'graph_search': 'knowledge_search',
}


def _safe_json_loads(raw: str) -> dict:
    """容错解析 LLM 输出的 JSON（兼容 ```json 包裹 / 首尾杂散字符）

    与 task_splitter.maybe_split 的解析策略保持一致：剥离代码块标记后
    尝试 json.loads，失败则截取首个 { 到最后一个 } 再试。
    """
    s = (raw or '').strip()
    if s.startswith('```'):
        s = s.strip('`')
        if s.lower().startswith('json'):
            s = s[4:]
        s = s.strip()
    try:
        return json.loads(s)
    except Exception:
        try:
            start = s.find('{')
            end = s.rfind('}')
            if start != -1 and end != -1 and end > start:
                return json.loads(s[start:end + 1])
        except Exception:
            pass
        return {}


def validate_dag(nodes: List[Dict], max_nodes: int = 10) -> tuple:
    """校验编排器产出的节点 DAG

    校验项：
    1. 节点数上限（防失控，与 AgentWorkflow.max_nodes 一致）
    2. 节点 id 唯一、id 非空字符串
    3. 节点类型合法（research / tool / approval）
    4. tool 节点的 tool_name 必须存在于工具注册表（防止编排器幻觉出不存在的工具）
    5. research 节点必须有 question；approval 节点必须有 reason
    6. depends_on 只能引用已声明的节点 id
    7. 无循环依赖（DFS 三色标记）

    Args:
        nodes: 编排器产出的节点列表
        max_nodes: 节点数上限

    Returns:
        (ok, errors)：ok=False 时 errors 为问题描述列表
    """
    errors: List[str] = []
    if not nodes:
        return True, []

    # 1. 节点数上限
    if len(nodes) > max_nodes:
        errors.append(f'节点数 {len(nodes)} 超过上限 {max_nodes}')

    # 2. id 唯一性
    ids = [n.get('id') for n in nodes]
    if any(not i for i in ids):
        errors.append('存在缺失的节点 id')
    if len(set(ids)) != len(ids):
        errors.append('节点 id 重复')

    # 3-5. 类型 / 字段 / 工具名
    tool_names = get_default_registry().names()
    for n in nodes:
        nid = n.get('id', '?')
        ntype = n.get('type')
        if ntype not in VALID_NODE_TYPES:
            errors.append(f'节点 {nid} 类型非法: {ntype}')
            continue
        if ntype == 'research' and not (n.get('question') or '').strip():
            errors.append(f'research 节点 {nid} 缺少 question')
        if ntype == 'approval' and not (n.get('reason') or '').strip():
            errors.append(f'approval 节点 {nid} 缺少 reason')
        if ntype == 'tool':
            tool_name = n.get('tool_name')
            if tool_name not in tool_names:
                errors.append(f'tool 节点 {nid} 工具不存在: {tool_name}')
            if not n.get('params') or not isinstance(n.get('params'), dict):
                errors.append(f'tool 节点 {nid} 缺少 params')

    # 6. depends_on 引用存在
    id_set = set(ids)
    for n in nodes:
        for dep in (n.get('depends_on') or []):
            if dep not in id_set:
                errors.append(f'节点 {n.get("id")} 引用了不存在的依赖: {dep}')

    # 7. 循环依赖检测（DFS 三色标记：0 未访问 / 1 访问中 / 2 已结束）
    def _has_cycle(nid: str, visiting: set, done: set) -> bool:
        if nid in done:
            return False
        if nid in visiting:
            return True  # 回到访问中节点 = 有环
        visiting.add(nid)
        node_map = {n.get('id'): n for n in nodes}
        for dep in (node_map.get(nid, {}).get('depends_on') or []):
            if dep in node_map and _has_cycle(dep, visiting, done):
                return True
        visiting.remove(nid)
        done.add(nid)
        return False

    for n in nodes:
        if _has_cycle(n.get('id'), set(), set()):
            errors.append('节点 DAG 存在循环依赖')
            break

    return not errors, errors


def maybe_plan(question: str, max_nodes: int = 10) -> Dict[str, Any]:
    """让 LLM 判断是否需要工作流并产出节点 DAG

    Args:
        question: 用户原始问题
        max_nodes: 节点数上限（默认 10，与 AgentWorkflow.max_nodes 一致）

    Returns:
        need_workflow=true 时：
            {'need_workflow': True, 'reason': str, 'nodes': [节点...]}
        need_workflow=false 时：
            {'need_workflow': False, 'reason': str}
        编排器输出异常/校验失败时按"不需要工作流"降级处理，
        保证复杂问题也能回落到现有 ReAct 单轮链路（可用性优先）。
    """
    llm = get_llm()
    msgs = [
        {'role': 'system', 'content': WORKFLOW_PLAN_SYSTEM},
        {'role': 'user', 'content': WORKFLOW_PLAN_USER_TEMPLATE.format(
            question=question, max_nodes=max_nodes)},
    ]
    try:
        resp = llm.chat(msgs, temperature=0.0, max_tokens=1200)
    except Exception as e:
        # LLM 不可用：降级为不拆分，走现有链路
        logger.warning(f'[WorkflowPlanner] llm chat error, degrade to simple path: {e}')
        return {'need_workflow': False, 'reason': 'planner llm error'}

    data = _safe_json_loads(resp.get('content') or '')
    if not data:
        logger.warning(f'[WorkflowPlanner] invalid json output: {(resp.get("content") or "")[:200]}')
        return {'need_workflow': False, 'reason': 'planner output invalid json'}

    if not data.get('need_workflow'):
        return {'need_workflow': False, 'reason': data.get('reason', '简单单一问题')}

    nodes = data.get('nodes') or []
    # 节点数上限由 LLM 侧约束 + 引擎侧硬校验双保险，这里按 max_nodes 截断
    nodes = nodes[:max_nodes]
    # 内部知识检索工具归一化：wiki_search/graph_search → knowledge_search
    # （knowledge_search 内部按 Wiki → 图谱 → 文档 三层固定顺序检索）
    for n in nodes:
        if n.get('type') == 'tool' and n.get('tool_name') in _DOC_UNIFIED_TOOLS:
            n['tool_name'] = _DOC_UNIFIED_TOOLS[n['tool_name']]
    ok, errors = validate_dag(nodes, max_nodes=max_nodes)
    if not ok:
        # 校验失败（如编排器幻觉出不存在的工具）：拒绝进入工作流，降级走现有链路
        logger.warning(f'[WorkflowPlanner] dag invalid, degrade to simple path: {errors}')
        return {'need_workflow': False, 'reason': f'dag invalid: {errors[0]}'}

    return {'need_workflow': True, 'reason': data.get('reason', ''), 'nodes': nodes}
