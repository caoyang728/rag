"""
apps.users.services.ticket_base - 权限配置审批工单服务 · 公共基座

审批规则（对齐 RAG_RBAC_权限架构设计.md 最终计划）：
- 同部门授权（GRANT team_leader/employee，目标用户与申请人同团队）：团队组长单审即可
- 跨部门/跨团队/全局角色：双轨审核（审核 + 复核）
- super_admin 新增/撤销：强制另一个 super_admin 双人复核
- 降级/撤销（REVOKE）：团队组长可直接执行，无需审批（但记审计）
- 任一节点 REJECTED → 工单终态 REJECTED，不执行授权表写入
- 审批工单永不删除，只改状态

工单流转状态机：
  PENDING --approve(末节点)--> APPROVED --execute(异步/同步)--> EXECUTED
  PENDING --reject--> REJECTED（终态）
  PENDING --cancel(发起人)--> CANCELLED（终态）

审批链 approval_chain 结构（JSONField，顺序执行，共享审批池模式）：
  [
    {"approver_role": "TEAM_LEADER", "status": "PENDING",
     "approver_id": null, "approved_at": null, "comment": ""},
    ...
  ]
  - approver_role：审批人角色定位（TEAM_LEADER / DEPT_LEADER / SUPER_ADMIN）
    创建时锁定角色类型，不锁定具体审批人（共享审批池 + 先到先得）
  - approver_id：审批时回填（谁先处理就锁定谁，防止并发审批）
  - status：PENDING / APPROVED / REJECTED
  - 顺序执行：current_step 指向待审批节点，前一节点 APPROVED 才到下一节点

本模块承载权限域与系统域共用的工单纯函数（审批链解析/双人独立性判定）、
工单号生成、流转日志与审计写入等公共能力，供 services 子包其余模块复用。
"""
import json
import re

from django.utils import timezone
from loguru import logger

from apps.users.models import (
    TicketList, TicketFlowLog, PermissionAuditLog,
    User, ScopeType, AuditTargetType, TicketBizType,
)


# ============================================================================
# 工单号生成（统一格式：类型前缀 + YYYYMMDD + 当日全局 4 位序列）
# ============================================================================
# 类型前缀（两字母大写拼音首字母）：权限 QX / 配置 PZ / 定时 DS / 模型 MX / 安全 AQ / 组织 ZZ
# 示例：QX202608080001（当日第 1 单，权限）、ZZ202608080001（当日第 1 单，组织变更）
TICKET_TYPE_PREFIX = {
    TicketBizType.PERMISSION: 'QX',
    TicketBizType.CONFIG: 'PZ',
    TicketBizType.SCHEDULE: 'DS',
    TicketBizType.MODEL: 'MX',
    TicketBizType.AGENT: 'AG',
    TicketBizType.SECURITY: 'AQ',
    TicketBizType.ORG: 'ZZ',
}

# 新格式工单号正则：两字母前缀 + 8 位日期 + 4 位当日序列（用于取当日全局序列）
_NEW_TICKET_NO_RE = re.compile(r'^[A-Z]{2}(\d{8})(\d{4})$')


# ============================================================================
# 审批人角色定位（用于审批链快照与前端展示）
# ============================================================================
class ApproverRole:
    """审批人在审批链中的角色定位 —— 决定该节点由谁审批

    新增 USER_ADMIN 后审批节点匹配规则:
    - TEAM_LEADER / DEPT_LEADER:基于组织架构匹配,带 scope 区分本团队/目标团队
    - TEAM_LEADER 判定 team.leader_id;DEPT_LEADER 判定 dept_manager 角色授权(见 _get_dept_leader_id)
    - USER_ADMIN:持有 user_admin 角色的用户(用于部门经理/文档管理员/合规管理员审批链)
    - KB_ADMIN:持有 kb_admin 角色的用户(用于部门级跨部门 viewer/contributor 授权审核)
    - SUPER_ADMIN:持有 super_admin 角色的用户(用于全局高权角色审批链 / 兜底)
    """
    TEAM_LEADER = 'TEAM_LEADER'    # 团队组长(单审 / 跨团队审核)
    DEPT_LEADER = 'DEPT_LEADER'    # 部门负责人(团队组长审批 / 跨部门复核)
    USER_ADMIN = 'USER_ADMIN'      # 用户管理员(部门经理/文档管理员/合规管理员审批链第一节点)
    KB_ADMIN = 'KB_ADMIN'          # 文档管理员(部门级跨部门 viewer/contributor 授权审核)
    SUPER_ADMIN = 'SUPER_ADMIN'    # 超级管理员(全局高权角色审批链第二节点 / super_admin 双人复核)
    WORKFLOW_OWNER = 'WORKFLOW_OWNER'  # Agent 工作流人工确认发起人(HITL 自助确认,超管兜底)


class ApproveStepStatus:
    """审批节点状态 —— 与 GrantStatus 解耦，仅用于审批链内部流转"""
    PENDING = 'PENDING'
    APPROVED = 'APPROVED'
    REJECTED = 'REJECTED'


# ============================================================================
# 团队级角色等级与 key 集合(同 scope 内高等级覆盖低等级,用于 ROLE_CHANGE 流向判定
# 与 create_ticket 团队级互斥自动转 ROLE_CHANGE;approval_chain 与 ticket_permission 共用)
# ============================================================================
# viewer < contributor < team_leader
TEAM_ROLE_RANK = {'viewer': 1, 'contributor': 2, 'team_leader': 3}
# 团队级角色 key 集合(同团队内互斥,用于 create_ticket 自动检测 ROLE_CHANGE)
TEAM_ROLE_KEYS = tuple(TEAM_ROLE_RANK.keys())


# ============================================================================
# 审计动作常量（统一从同包 audit_service.AuditAction 导入，消除重复定义）
# ============================================================================
from .audit_service import AuditAction


# ============================================================================
# 权限域与系统域共用的工单纯函数（序列化 / 双人独立性判定）
# ============================================================================

def parse_change_summary(cs_raw):
    """解析 change_summary 字段 —— 兼容 JSON 字符串与已解析对象

    config/schedule 工单的 change_summary 存为 JSON 字符串，列表/详情序列化
    需解析后返回对象供前端渲染；解析失败（脏数据）返回 None 而不是抛异常，
    前端按"无差异摘要"处理，不影响审批流程展示。
    """
    if not cs_raw:
        return None
    try:
        return json.loads(cs_raw) if isinstance(cs_raw, str) else cs_raw
    except (json.JSONDecodeError, TypeError):
        return None


def get_approved_approver_ids(ticket) -> set:
    """返回工单已审前序节点的审批人 ID 集合 —— 双人独立性的公共判定

    业务背景：双审/双超管工单要求前序节点审批人不能再审后续节点，
    权限域 _can_approve_for_role 与系统域 SUPER_ADMIN 复核/驳回共用同一判定，
    避免两处各写一份逻辑漂移。审批链为空或工单未走完时返回空集。
    """
    if not ticket or not getattr(ticket, 'approval_chain', None):
        return set()
    return {
        n.get('approver_id') for n in ticket.approval_chain[:ticket.current_step]
        if n.get('approver_id')
    }


# ============================================================================
# 工单创建与流转（公共：工单号生成 / 流转日志 / 审计写入）
# ============================================================================

def _gen_ticket_no(biz_type: str = TicketBizType.PERMISSION) -> str:
    """生成统一格式工单号：类型前缀 + YYYYMMDD + 当日全局 4 位序列

    当日全局序列 = 当日已存在的新格式工单最大序号 + 1（跨类型共享当日序号，
    与工单号示例 QX…0001 / DS…0002 / PZ…0003 一致）。唯一性由 ticket_no 唯一索引兜底。
    """
    prefix = TICKET_TYPE_PREFIX.get(biz_type, 'QX')
    today = timezone.localtime().strftime('%Y%m%d')
    # 一次查询取出最新一条新格式工单号（新格式排序即按日期+序号降序）
    last_no = TicketList.objects.filter(
        ticket_no__regex=r'^[A-Z]{2}\d{12}$',
    ).order_by('-ticket_no').values_list('ticket_no', flat=True).first()
    seq = 1
    if last_no:
        m = _NEW_TICKET_NO_RE.match(last_no)
        if m and m.group(1) == today:
            seq = int(m.group(2)) + 1
    return f'{prefix}{today}{seq:04d}'


def _log_flow(ticket: TicketList, action: str, actor: User = None,
              comment: str = '', step: int = 0):
    """写流转日志 —— 工单业务对象的一部分（事务内写入，随工单回滚）

    action: SUBMIT(提交) / APPROVE(节点通过) / REJECT(驳回) / CANCEL(撤回) / EXECUTE(执行)
    与审计日志（PermissionAuditLog）分离：流转日志随工单生命周期，审计日志只增不删。
    """
    TicketFlowLog.objects.create(
        ticket=ticket,
        action=action,
        actor=actor,
        step=step,
        comment=comment or '',
    )


def _write_audit(ticket: TicketList, actor: User, action: str,
                 ip_address: str, user_agent: str, result: str = 'SUCCESS', extra: dict = None):
    """写权限审计日志 —— 工单全生命周期留痕

    target_type=TICKET，target_id=ticket.id，便于按工单反查所有审计事件。
    extra 合并到 after_snapshot，记录节点/评论等上下文。

    审计可丢、业务不可丢：写入失败仅记日志，绝不向上抛异常。
    """
    after = {'ticket_no': ticket.ticket_no, 'change_type': getattr(ticket, 'change_type', None),
             'status': ticket.status}
    if extra:
        after.update(extra)
    try:
        PermissionAuditLog.objects.create(
            actor=actor,
            action=action,
            target_type=AuditTargetType.TICKET,
            target_id=ticket.id,
            target_user=getattr(ticket, 'target_user', None),
            role=getattr(ticket, 'role', None),
            scope_type=getattr(ticket, 'scope_type', None) or ScopeType.NONE,
            scope_id=getattr(ticket, 'scope_id', None),
            after_snapshot=after,
            result=result,
            ip_address=ip_address or None,
            user_agent=user_agent or '',
        )
    except Exception as exc:
        # 审计写入失败不得阻断主业务：仅记日志便于运维排查，不向上抛出
        logger.error(
            f'[Audit] _write_audit failed (不阻断主业务): action={action} '
            f'ticket_no={ticket.ticket_no} actor={getattr(actor, "id", None)} '
            f'result={result} err={exc}'
        )
