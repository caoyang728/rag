"""
Human-in-the-Loop（HITL）模块 —— 工作流人工确认节点

- create_approval_ticket()：审批节点触发时创建统一审批工单（biz_type=agent，
  详情子表 TicketAgentApprovalDetail，审批链 = 单节点 WORKFLOW_OWNER 自助确认）
- resume_workflow_from_ticket()：工单进入终态后（users/views.py 审批/驳回钩子调用）
  恢复工作流执行：通过 → 节点 approved 继续；驳回 → 节点 rejected 降级/中止

设计（复用统一工单系统，遵循"工单永不删除只改状态"）：
- reason 前缀固定为 [agent:{wf_id}:approval]，供审计与工单中心区分 HITL 工单
- 工单由发起人（工作流用户）本人确认（WORKFLOW_OWNER），超管兜底
- 审批通过/驳回后同步恢复工作流；恢复失败仅记日志，不影响审批结果落库
"""
from loguru import logger
from django.db import transaction
from django.utils import timezone

from apps.users.models import TicketList, TicketStatus, TicketBizType, TicketAgentApprovalDetail
from apps.users import ticket_service

# reason 前缀约定：审计/工单中心据此识别 HITL 工单来源
HITL_REASON_PREFIX = '[agent:{wf_id}:approval]'


def _build_hitl_chain() -> list:
    """构造 HITL 单节点审批链（WORKFLOW_OWNER 自助确认）

    单节点即可：确认行为由发起人本人负责，无需组织层级审批；
    超管兜底已由 _can_approve_for_role 的 WORKFLOW_OWNER 分支保证。
    """
    return [ticket_service._build_chain_node(ticket_service.ApproverRole.WORKFLOW_OWNER)]


@transaction.atomic
def create_approval_ticket(workflow, node: dict, user) -> TicketList:
    """为工作流审批节点创建人工确认工单

    Args:
        workflow: AgentWorkflow 实例
        node: 审批节点定义（{id, name, type='approval', reason, ...}）
        user: 工作流发起人（工单 applicant，即 HITL 确认人）

    Returns:
        创建的 TicketList 工单（PENDING）
    """
    reason = node.get('reason') or f'工作流 {workflow.id} 节点 {node.get("id")} 需人工确认'
    ticket = TicketList.objects.create(
        ticket_no=ticket_service._gen_ticket_no(TicketBizType.AGENT),
        title=f'Agent工作流确认·{node.get("name") or node.get("id")}',
        biz_type=TicketBizType.AGENT,
        status=TicketStatus.PENDING,
        risk_level='normal',
        applicant=user,
        approval_chain=_build_hitl_chain(),
        current_step=0,
        operation='agent_approval',
    )
    TicketAgentApprovalDetail.objects.create(
        ticket=ticket,
        workflow_id=workflow.id,
        node_id=node.get('id'),
        reason=f'{HITL_REASON_PREFIX.format(wf_id=workflow.id)} {reason}',
    )
    ticket_service._log_flow(ticket, 'SUBMIT', actor=user)
    logger.info(f'[WorkflowHitl] approval ticket created: {ticket.ticket_no} '
                f'workflow={workflow.id} node={node.get("id")}')
    return ticket


def resume_workflow_from_ticket(ticket: TicketList):
    """工单进入终态后恢复工作流执行（users/views.py 审批/驳回钩子调用）

    根据工单终态决定恢复方向：
    - EXECUTED/APPROVED：人工确认通过 → 节点 approved → 工作流继续
    - REJECTED：人工拒绝 → 节点 rejected → 工作流降级/中止
    - CANCELLED/其他：无恢复动作（工单被撤回/异常终态）

    同步执行：审批请求线程内完成，避免审批通过后工作流长期悬挂；
    恢复失败只记日志，不影响审批结果落库（审计可丢、业务不可丢的权衡）。
    """
    detail = getattr(ticket, 'agent_approval_detail', None)
    if not detail:
        logger.warning(f'[WorkflowHitl] ticket {ticket.id} has no agent_approval_detail')
        return
    try:
        from apps.agent.models import AgentWorkflow
        from apps.agent.workflow.engine import resume_workflow
        workflow = AgentWorkflow.objects.filter(id=detail.workflow_id).first()
        if not workflow:
            logger.warning(f'[WorkflowHitl] workflow {detail.workflow_id} not found, skip resume')
            return
        if workflow.status != 'waiting_approval':
            logger.info(f'[WorkflowHitl] workflow {workflow.id} status={workflow.status}, skip resume')
            return
        approved = ticket.status in (TicketStatus.EXECUTED, TicketStatus.APPROVED)
        resume_workflow(workflow, node_id=detail.node_id, approved=approved)
    except Exception:
        # 恢复失败仅记录日志：审批结果已落库，工作流停留在 waiting_approval 可由运维干预
        logger.exception(f'[WorkflowHitl] resume workflow failed, ticket={ticket.id}')
