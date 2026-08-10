"""
apps.agent.workflow - 多 Agent 工作流编排子系统

职责划分（参考 LangGraph / 字节 Coze 等大厂的"编排器 + 执行器"分层）：
- planner：编排器。LLM 判断问题是否需要工作流，产出节点 DAG（research/tool/approval）
- engine：执行器。按拓扑序运行 DAG，支持并行、失败重试、时长/节点数上限、审批阻断与降级
- hitl：Human-in-the-Loop。审批节点复用统一审批工单（biz_type=agent），
  审批通过前工作流停留 waiting_approval，通过后恢复、拒绝后降级

对外统一入口：
    from apps.agent.workflow.engine import run_workflow_stream
    from apps.agent.workflow.planner import maybe_plan
"""
