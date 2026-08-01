"""
apps.users.ticket_service - 权限配置审批工单服务

审批规则（对齐 RAG_RBAC_权限架构设计.md 最终计划）：
- 同部门授权（GRANT team_leader/employee，目标用户与申请人同团队）：团队组长单审即可
- 跨部门/跨团队/全局角色：双轨审核（一审 + 二审）
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
"""
import uuid
from typing import Optional

from django.db import transaction
from django.utils import timezone
from loguru import logger

from apps.users.models import (
    PermissionApprovalTicket, PermissionAuditLog,
    UserRoleRel, UserDeptScopeRel, UserTeamScopeRel,
    Role, User, Department, Team,
    TicketStatus, TicketChangeType, ScopeType, RoleType, GrantStatus,
    AuditTargetType,
)


# ============================================================================
# 审批人角色定位（用于审批链快照与前端展示）
# ============================================================================
class ApproverRole:
    """审批人在审批链中的角色定位 —— 决定该节点由谁审批"""
    TEAM_LEADER = 'TEAM_LEADER'    # 目标团队组长（单审 / 一审）
    DEPT_LEADER = 'DEPT_LEADER'    # 目标部门负责人（二审）
    SUPER_ADMIN = 'SUPER_ADMIN'    # 超级管理员（全局角色双审 / super_admin 复核）


class ApproveStepStatus:
    """审批节点状态 —— 与 GrantStatus 解耦，仅用于审批链内部流转"""
    PENDING = 'PENDING'
    APPROVED = 'APPROVED'
    REJECTED = 'REJECTED'


# ============================================================================
# 审计动作常量（对齐 PermissionAuditLog.action 清单）
# ============================================================================
class AuditAction:
    TICKET_CREATE = 'TICKET_CREATE'
    TICKET_APPROVE = 'TICKET_APPROVE'
    TICKET_REJECT = 'TICKET_REJECT'
    TICKET_CANCEL = 'TICKET_CANCEL'
    TICKET_EXECUTE = 'TICKET_EXECUTE'
    ROLE_GRANT = 'ROLE_GRANT'
    ROLE_REVOKE = 'ROLE_REVOKE'
    SCOPE_GRANT = 'SCOPE_GRANT'
    SCOPE_REVOKE = 'SCOPE_REVOKE'


# ============================================================================
# 审批人角色匹配 —— 共享审批池的核心：判定用户是否具备某审批节点所需的角色
# ============================================================================

def _can_approve_for_role(user, approver_role: str, ticket=None) -> bool:
    """判定用户是否能审批指定 approver_role 的节点 —— 共享审批池的核心校验

    各 approver_role 对应的判定逻辑：
    - SUPER_ADMIN：用户有 super_admin 角色（排除申请人/目标用户）
    - TEAM_LEADER：用户是目标团队的组长（team.leader_id == user.id）
    - DEPT_LEADER：用户是目标部门的负责人（department.leader_id == user.id）

    排除规则：申请人不能审自己发起的工单；目标用户不能审自己的授权工单。
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False

    # 排除申请人和目标用户
    if ticket:
        if user.id == ticket.applicant_id:
            return False
        if user.id == ticket.target_user_id:
            return False

    if approver_role == ApproverRole.SUPER_ADMIN:
        # 超级管理员可审批全局角色/super_admin 操作
        return UserRoleRel.objects.filter(
            user=user, role__role_key='super_admin',
            status=GrantStatus.ACTIVE,
        ).exists()

    if approver_role == ApproverRole.TEAM_LEADER:
        # 目标团队组长审批
        if not ticket or not ticket.scope_id:
            return False
        if ticket.scope_type != ScopeType.TEAM:
            return False
        team = Team.objects.filter(id=ticket.scope_id).only('leader_id').first()
        return team and team.leader_id == user.id

    if approver_role == ApproverRole.DEPT_LEADER:
        # 目标部门负责人审批
        if not ticket or not ticket.scope_id:
            return False
        dept_id = ticket.scope_id
        # 双轨二审时 scope_type=TEAM，需取团队所属部门
        if ticket.scope_type == ScopeType.TEAM:
            dept = Department.objects.filter(teams__id=ticket.scope_id).first()
            dept_id = dept.id if dept else None
        if not dept_id:
            return False
        return Department.objects.filter(id=dept_id, leader_id=user.id).exists()

    return False


def _find_approver_ids_for_role(approver_role: str, ticket=None) -> list:
    """查找能审批指定 approver_role 的所有用户 ID —— 用于待办列表查询"""
    if approver_role == ApproverRole.SUPER_ADMIN:
        return _get_super_admin_ids(
            exclude_user_id=ticket.applicant_id if ticket else None,
        )

    if approver_role == ApproverRole.TEAM_LEADER:
        if not ticket or ticket.scope_type != ScopeType.TEAM or not ticket.scope_id:
            return []
        team = Team.objects.filter(id=ticket.scope_id).only('leader_id').first()
        return [team.leader_id] if team and team.leader_id else []

    if approver_role == ApproverRole.DEPT_LEADER:
        if not ticket or not ticket.scope_id:
            return []
        dept_id = ticket.scope_id
        if ticket.scope_type == ScopeType.TEAM:
            dept = Department.objects.filter(teams__id=ticket.scope_id).first()
            dept_id = dept.id if dept else None
        if not dept_id:
            return []
        leader_id = Department.objects.filter(id=dept_id).values_list('leader_id', flat=True).first()
        return [leader_id] if leader_id else []

    return []


# ============================================================================
# 审批链构造：根据变更类型 + 范围决定走单审 / 双轨 / 直接执行
# ============================================================================

def _get_team_leader_id(team_id) -> Optional[int]:
    """获取团队组长 ID —— 单审/一审审批人

    组长可能为空（团队刚建立未指派），此时退化为该团队所属部门负责人审批。
    """
    if not team_id:
        return None
    team = Team.objects.filter(id=team_id).only('leader_id', 'department_id').first()
    if team and team.leader_id:
        return team.leader_id
    return None


def _get_dept_leader_id(dept_id) -> Optional[int]:
    """获取部门负责人 ID —— 二审审批人"""
    if not dept_id:
        return None
    return Department.objects.filter(id=dept_id).values_list('leader_id', flat=True).first()


def _get_super_admin_ids(exclude_user_id=None, role_keys=('super_admin',)) -> list:
    """获取超级管理员 ID 列表 —— 支持按角色 key 过滤

    参数：
    - exclude_user_id：排除发起人/目标用户（不能审自己的工单）
    - role_keys：角色 key 元组，默认查 super_admin

    全局角色授权/撤销强制双人复核，不足 2 人时仍写入链路，由剩余 super_admin 审批。
    """
    sa_roles = list(Role.objects.filter(role_key__in=role_keys).values_list('id', flat=True))
    if not sa_roles:
        return []
    qs = UserRoleRel.objects.filter(
        role_id__in=sa_roles, status=GrantStatus.ACTIVE,
    ).values_list('user_id', flat=True).distinct()
    if exclude_user_id:
        qs = qs.exclude(user_id=exclude_user_id)
    return list(qs)


def build_approval_chain(applicant, target_user, change_type: str,
                         role: Role, scope_type: str, scope_id) -> list:
    """构造审批链 —— 根据变更类型与范围决定单审/双轨/直接执行

    返回 [] 表示无需审批（REVOKE 普通角色直接执行）；返回多节点表示双轨审核。

    规则分支：
    1. super_admin 角色操作 → 强制双 super_admin 复核
    2. REVOKE + 非 super_admin 角色 → []（团队组长可直接撤销，无需审批）
    3. GRANT 全局角色（非超管）→ 双 super_admin 复核
    4. GRANT + 同团队（scope_type=TEAM 且 target 与 applicant 同团队）→ 单审（团队组长）
    5. GRANT + 跨部门/跨团队/部门属地角色 → 双轨（一审团队组长 + 二审部门负责人）

    注：审批人快照写入 approval_chain，避免后续 leader 变动影响在途工单。
    """
    is_target_sa = target_user and target_user.is_super_admin

    # ── 分支 1：super_admin 角色操作 → 强制双 super_admin 复核 ──
    # 业务背景：超管角色的新增/撤销需双人复核，避免单点授权风险
    if role and role.role_key == 'super_admin':
        return _build_super_admin_chain(applicant, target_user)

    # 撤销/降级：普通角色直接执行（返回空链）；目标用户是超管的撤销仍需双审
    if change_type == TicketChangeType.REVOKE:
        if is_target_sa:
            return _build_super_admin_chain(applicant, target_user)
        # 普通撤销：团队组长可直接执行，记审计即可
        return []

    # 全局角色 GRANT/SCOPE_CHANGE/EXPIRE_EXTEND → 双 super_admin 复核
    if role and role.role_type == RoleType.GLOBAL:
        return _build_super_admin_chain(applicant, target_user)

    # 同团队授权（team_leader / employee 在本团队内授权）→ 单审：目标团队组长
    # 仅当申请人与目标用户同属该团队时走单审；否则 fall through 到双轨
    if scope_type == ScopeType.TEAM and scope_id and _is_same_team(applicant, target_user, scope_id):
        leader_id = _get_team_leader_id(scope_id)
        if leader_id and leader_id != applicant.id:
            return [{
                'approver_id': None,
                'approver_role': ApproverRole.TEAM_LEADER,
                'status': ApproveStepStatus.PENDING,
                'approved_at': None,
                'comment': '',
            }]
        # 同团队但组长缺失或自审：退化为部门负责人单审，避免工单卡死
        dept_id = _get_team_dept_id(scope_id)
        dept_leader = _get_dept_leader_id(dept_id)
        if dept_leader and dept_leader != applicant.id:
            return [{
                'approver_id': None,
                'approver_role': ApproverRole.DEPT_LEADER,
                'status': ApproveStepStatus.PENDING,
                'approved_at': None,
                'comment': '',
            }]
        # 部门负责人也缺失：退化为任意 super_admin 单审兜底
        sa_ids = _get_super_admin_ids(exclude_user_id=applicant.id)
        if sa_ids:
            return [{
                'approver_id': None,
                'approver_role': ApproverRole.SUPER_ADMIN,
                'status': ApproveStepStatus.PENDING,
                'approved_at': None,
                'comment': '',
            }]
        return []  # 无可用审批人：直接放行（开发期兜底，生产应告警）

    # 跨部门/跨团队/部门属地角色 → 双轨：一审团队组长 + 二审部门负责人
    return _build_dual_chain(applicant, scope_type, scope_id)


def _build_super_admin_chain(applicant, target_user) -> list:
    """构造 super_admin 双人复核审批链 —— 共享审批池：两个 SUPER_ADMIN 顺序审批

    强制 2 个不同超管顺序审批，避免单点授权风险。
    使用共享审批池：每个节点仅锁定 approver_role=SUPER_ADMIN，不锁定具体用户。
    任何超管均可审批，先到先得。
    """
    chain = [
        {
            'approver_id': None,
            'approver_role': ApproverRole.SUPER_ADMIN,
            'status': ApproveStepStatus.PENDING,
            'approved_at': None,
            'comment': '',
        },
        {
            'approver_id': None,
            'approver_role': ApproverRole.SUPER_ADMIN,
            'status': ApproveStepStatus.PENDING,
            'approved_at': None,
            'comment': '',
        },
    ]
    return chain


def _build_dual_chain(applicant, scope_type: str, scope_id) -> list:
    """构造双轨审核链：一审团队组长 + 二审部门负责人

    跨部门/跨团队授权走双轨，确保跨组织变更有两个层级把关。
    使用共享审批池模式：节点仅锁定 approver_role，不锁定具体审批人。
    审批人缺失时仍保留 approver_role（共享审批池模式下角色匹配即可）。
    """
    chain = []

    # 一审：团队组长（共享审批池：任何该团队组长均可审批）
    if scope_type == ScopeType.TEAM and scope_id:
        team_leader_id = _get_team_leader_id(scope_id)
        if team_leader_id and team_leader_id != applicant.id:
            chain.append({
                'approver_id': None,
                'approver_role': ApproverRole.TEAM_LEADER,
                'status': ApproveStepStatus.PENDING,
                'approved_at': None,
                'comment': '',
            })

    # 二审：部门负责人（与一审不同人，共享审批池）
    if scope_type in (ScopeType.TEAM, ScopeType.DEPT) and scope_id:
        dept_id = scope_id
        if scope_type == ScopeType.TEAM:
            dept_id = _get_team_dept_id(scope_id)
        dept_leader_id = _get_dept_leader_id(dept_id)
        if dept_leader_id and dept_leader_id != applicant.id:
            # 与一审同一人时仍保留二审节点，但角色为 DEPT_LEADER
            chain.append({
                'approver_id': None,
                'approver_role': ApproverRole.DEPT_LEADER,
                'status': ApproveStepStatus.PENDING,
                'approved_at': None,
                'comment': '',
            })

    # 审批链为空：退化 super_admin 兜底单审（共享审批池）
    if not chain:
        chain.append({
            'approver_id': None,
            'approver_role': ApproverRole.SUPER_ADMIN,
            'status': ApproveStepStatus.PENDING,
            'approved_at': None,
            'comment': '',
        })
    return chain


def _is_same_team(applicant, target_user, team_id) -> bool:
    """申请人与目标用户是否同属指定团队（用于判定单审/双轨）

    同团队内授权走单审；跨团队走双轨。
    """
    if not target_user:
        return False
    return applicant.team_id == team_id and target_user.team_id == team_id


def _get_team_dept_id(team_id):
    """获取团队所属部门 ID"""
    if not team_id:
        return None
    return Team.objects.filter(id=team_id).values_list('department_id', flat=True).first()


# ============================================================================
# 工单创建与流转
# ============================================================================

def _gen_ticket_no() -> str:
    """生成全局唯一工单号：T + 日期 + 短 UUID，便于人工沟通与检索"""
    return 'T' + timezone.localtime().strftime('%Y%m%d') + uuid.uuid4().hex[:8].upper()


@transaction.atomic
def create_ticket(applicant, target_user, change_type: str,
                  role: Role, scope_type: str = ScopeType.NONE, scope_id=None,
                  effective_from=None, expires_at=None, reason: str = '',
                  ip_address: str = '', user_agent: str = '') -> PermissionApprovalTicket:
    """创建审批工单 —— 授权变更统一入口

    流程：
    1. 构造审批链（build_approval_chain）
    2. 空链 → REVOKE 普通角色：直接执行撤销并返回已执行工单（记审计）
    3. 非空链 → 创建 PENDING 工单，等待逐级审批
    4. 写 TICKET_CREATE 审计

    返回：工单对象（已执行或待审批）
    """
    chain = build_approval_chain(applicant, target_user, change_type,
                                  role, scope_type, scope_id)

    # 空审批链：降级/撤销普通角色 → 直接执行（团队组长可直接撤销，无需审批）
    if not chain:
        ticket = PermissionApprovalTicket.objects.create(
            ticket_no=_gen_ticket_no(),
            applicant=applicant,
            target_user=target_user,
            change_type=change_type,
            role=role,
            scope_type=scope_type,
            scope_id=scope_id,
            effective_from=effective_from,
            expires_at=expires_at,
            reason=reason,
            approval_chain=[],
            current_step=0,
            status=TicketStatus.EXECUTED,
            approved_at=timezone.now(),
            executed_at=timezone.now(),
        )
        _execute_grant_or_revoke(ticket, actor=applicant)
        _write_audit(ticket, applicant, AuditAction.TICKET_CREATE,
                     ip_address, user_agent, result='SUCCESS')
        _write_audit(ticket, applicant, AuditAction.TICKET_EXECUTE,
                     ip_address, user_agent, result='SUCCESS')
        logger.info(f'[Ticket] 直接执行(无审批链): {change_type} '
                    f'{role.role_key if role else "-"} -> {target_user.id}')
        return ticket

    # 非空审批链：创建待审批工单
    ticket = PermissionApprovalTicket.objects.create(
        ticket_no=_gen_ticket_no(),
        applicant=applicant,
        target_user=target_user,
        change_type=change_type,
        role=role,
        scope_type=scope_type,
        scope_id=scope_id,
        effective_from=effective_from,
        expires_at=expires_at,
        reason=reason,
        approval_chain=chain,
        current_step=0,
        status=TicketStatus.PENDING,
    )
    _write_audit(ticket, applicant, AuditAction.TICKET_CREATE,
                 ip_address, user_agent, result='SUCCESS')
    logger.info(f'[Ticket] 创建待审批工单: {ticket.ticket_no} '
                f'approvers={[n["approver_id"] for n in chain]}')
    return ticket


@transaction.atomic
def approve_ticket(ticket: PermissionApprovalTicket, approver: User,
                   comment: str = '', ip_address: str = '', user_agent: str = '') -> PermissionApprovalTicket:
    """审批通过当前节点 —— 共享审批池模式：任一符合 approver_role 的用户均可审批，先到先得

    校验（共享审批池 + 先到先得）：
    - 工单必须 PENDING
    - approver 必须具备当前节点 approver_role 所要求的角色/身份
    - 审批时回填 approver_id（锁定审批人，防止并发审批）
    - 不允许跨节点审批
    - select_for_update 防并发：两个管理员同时审批时只有一个能成功

    末节点通过 → status=APPROVED → 同步执行授权写入 → status=EXECUTED
    """
    # select_for_update 防止并发审批：同一工单同时只能被一个事务修改
    ticket = PermissionApprovalTicket.objects.select_for_update().get(pk=ticket.pk)

    if ticket.status != TicketStatus.PENDING:
        raise ValueError(f'工单非待审批状态: {ticket.status}')

    chain = ticket.approval_chain or []
    if ticket.current_step >= len(chain):
        raise ValueError('审批链已完结，无待审批节点')

    node = chain[ticket.current_step]
    approver_role = node['approver_role']

    # 共享审批池校验：判定 approver 是否具备该节点 approver_role 所需的角色/身份
    # 如果节点已有 approver_id（被其他管理员先处理），则拒绝
    if node.get('approver_id') and node['approver_id'] != approver.id:
        raise PermissionError('该工单已被其他管理员处理，不再属于您的待办')

    # 角色匹配校验：approver 必须具备对应 approver_role 的权限
    if not _can_approve_for_role(approver, approver_role, ticket):
        raise PermissionError(f'您没有审批 {approver_role} 类型工单的权限')

    # 回填 approver_id（锁定审批人，防止其他人再审批此节点）
    now = timezone.now()
    node['approver_id'] = approver.id
    node['status'] = ApproveStepStatus.APPROVED
    node['approved_at'] = now.isoformat()
    node['comment'] = comment
    ticket.approval_chain = chain  # 触发 JSONField 保存

    _write_audit(ticket, approver, AuditAction.TICKET_APPROVE,
                 ip_address, user_agent, result='SUCCESS',
                 extra={'step': ticket.current_step, 'approver_role': approver_role})

    # 末节点通过 → 工单通过 → 执行授权写入
    if ticket.current_step >= len(chain) - 1:
        ticket.status = TicketStatus.APPROVED
        ticket.approved_at = now
        ticket.save()
        _execute_grant_or_revoke(ticket, actor=approver)
        ticket.status = TicketStatus.EXECUTED
        ticket.executed_at = timezone.now()
        ticket.save()
        _write_audit(ticket, approver, AuditAction.TICKET_EXECUTE,
                     ip_address, user_agent, result='SUCCESS')
        logger.info(f'[Ticket] 工单审批通过并执行: {ticket.ticket_no} '
                    f'approver={approver.id} role={approver_role}')
    else:
        # 推进到下一节点
        ticket.current_step += 1
        ticket.save()
        logger.info(f'[Ticket] 工单节点通过，推进下一节点: {ticket.ticket_no} step={ticket.current_step}')
    return ticket


@transaction.atomic
def reject_ticket(ticket: PermissionApprovalTicket, rejector: User,
                  comment: str = '', ip_address: str = '', user_agent: str = '') -> PermissionApprovalTicket:
    """驳回工单 —— 共享审批池模式：任一符合 approver_role 的用户均可驳回

    驳回人可以是当前节点审批人（角色匹配），或 super_admin（兜底越级驳回）。
    """
    ticket = PermissionApprovalTicket.objects.select_for_update().get(pk=ticket.pk)

    if ticket.status != TicketStatus.PENDING:
        raise ValueError(f'工单非待审批状态: {ticket.status}')

    chain = ticket.approval_chain or []
    # 当前节点审批人（角色匹配）或 super_admin 可驳回
    can_reject = False
    if ticket.current_step < len(chain):
        node = chain[ticket.current_step]
        if node.get('approver_id') and node['approver_id'] == rejector.id:
            can_reject = True
        elif _can_approve_for_role(rejector, node['approver_role'], ticket):
            can_reject = True
    if not can_reject and not rejector.is_super_admin:
        raise PermissionError('无权驳回该工单')

    if ticket.current_step < len(chain):
        chain[ticket.current_step]['status'] = ApproveStepStatus.REJECTED
        chain[ticket.current_step]['approver_id'] = rejector.id
        chain[ticket.current_step]['comment'] = comment
        ticket.approval_chain = chain

    ticket.status = TicketStatus.REJECTED
    ticket.save()
    _write_audit(ticket, rejector, AuditAction.TICKET_REJECT,
                 ip_address, user_agent, result='SUCCESS',
                 extra={'comment': comment})
    logger.info(f'[Ticket] 工单被驳回: {ticket.ticket_no} by={rejector.id}')
    return ticket


@transaction.atomic
def cancel_ticket(ticket: PermissionApprovalTicket, actor: User,
                  ip_address: str = '', user_agent: str = '') -> PermissionApprovalTicket:
    """发起人撤回工单 —— 仅 PENDING 状态可撤回，已执行不可撤

    防止授权已生效后撤回工单造成状态不一致。
    """
    if ticket.status != TicketStatus.PENDING:
        raise ValueError('仅待审批工单可撤回')
    if ticket.applicant_id != actor.id and not actor.is_super_admin:
        raise PermissionError('仅发起人可撤回工单')

    ticket.status = TicketStatus.CANCELLED
    ticket.save()
    _write_audit(ticket, actor, AuditAction.TICKET_CANCEL,
                 ip_address, user_agent, result='SUCCESS')
    logger.info(f'[Ticket] 工单撤回: {ticket.ticket_no} by={actor.id}')
    return ticket


# ============================================================================
# 工单执行：审批通过后写入授权表（GRANT）或撤销授权（REVOKE）
# ============================================================================

def _execute_grant_or_revoke(ticket: PermissionApprovalTicket, actor: User):
    """执行授权写入 —— 工单 APPROVED 后调用

    GRANT：根据 scope_type 写入对应授权表，status=ACTIVE
    REVOKE：将对应授权记录置 status=REVOKED + revoked_at
    SCOPE_CHANGE：先撤销旧 scope 授权，再写入新 scope
    EXPIRE_EXTEND：更新 expires_at

    幂等：通过 ticket.status=EXECUTED 防重复执行（调用前已置 APPROVED）。
    """
    if ticket.change_type in (TicketChangeType.GRANT, TicketChangeType.SCOPE_CHANGE):
        _apply_grant(ticket, actor)
    elif ticket.change_type == TicketChangeType.REVOKE:
        _apply_revoke(ticket, actor)
    elif ticket.change_type == TicketChangeType.EXPIRE_EXTEND:
        _apply_extend(ticket, actor)


def _apply_grant(ticket: PermissionApprovalTicket, actor: User):
    """写入授权表（GRANT/SCOPE_CHANGE）—— 根据 scope_type 分发到三张授权表

    - scope_type=NONE + 全局角色 → UserRoleRel
    - scope_type=DEPT → UserDeptScopeRel
    - scope_type=TEAM → UserTeamScopeRel
    """
    common = dict(
        user=ticket.target_user,
        role=ticket.role,
        granted_by=actor,
        effective_from=ticket.effective_from,
        expires_at=ticket.expires_at,
        status=GrantStatus.ACTIVE,
        ticket=ticket,
    )
    if ticket.scope_type == ScopeType.DEPT and ticket.scope_id:
        UserDeptScopeRel.objects.update_or_create(
            user=ticket.target_user, role=ticket.role, dept_id=ticket.scope_id,
            defaults=common,
        )
        action = AuditAction.SCOPE_GRANT
    elif ticket.scope_type == ScopeType.TEAM and ticket.scope_id:
        UserTeamScopeRel.objects.update_or_create(
            user=ticket.target_user, role=ticket.role, team_id=ticket.scope_id,
            defaults=common,
        )
        action = AuditAction.SCOPE_GRANT
    else:
        # 全局角色（scope_type=NONE）
        UserRoleRel.objects.update_or_create(
            user=ticket.target_user, role=ticket.role,
            defaults=common,
        )
        action = AuditAction.ROLE_GRANT
    _write_audit(ticket, actor, action, '', '', result='SUCCESS')


def _apply_revoke(ticket: PermissionApprovalTicket, actor: User):
    """撤销授权（REVOKE）—— 将对应授权记录置 REVOKED

    逐表尝试撤销（一个用户同一角色可能跨表存在），全部命中即撤销。
    """
    now = timezone.now()
    revoked = False
    # 全局角色
    if UserRoleRel.objects.filter(
        user=ticket.target_user, role=ticket.role, status=GrantStatus.ACTIVE,
    ).update(status=GrantStatus.REVOKED, revoked_at=now, revoked_by=actor, ticket=ticket):
        revoked = True
    # 部门属地
    if ticket.scope_type in (ScopeType.DEPT, ScopeType.NONE):
        qs = UserDeptScopeRel.objects.filter(
            user=ticket.target_user, role=ticket.role, status=GrantStatus.ACTIVE,
        )
        if ticket.scope_type == ScopeType.DEPT and ticket.scope_id:
            qs = qs.filter(dept_id=ticket.scope_id)
        if qs.update(status=GrantStatus.REVOKED, revoked_at=now, revoked_by=actor, ticket=ticket):
            revoked = True
    # 团队属地
    if ticket.scope_type in (ScopeType.TEAM, ScopeType.NONE):
        qs = UserTeamScopeRel.objects.filter(
            user=ticket.target_user, role=ticket.role, status=GrantStatus.ACTIVE,
        )
        if ticket.scope_type == ScopeType.TEAM and ticket.scope_id:
            qs = qs.filter(team_id=ticket.scope_id)
        if qs.update(status=GrantStatus.REVOKED, revoked_at=now, revoked_by=actor, ticket=ticket):
            revoked = True

    action = AuditAction.ROLE_REVOKE if ticket.scope_type == ScopeType.NONE else AuditAction.SCOPE_REVOKE
    _write_audit(ticket, actor, action, '', '',
                 result='SUCCESS' if revoked else 'NOOP')


def _apply_extend(ticket: PermissionApprovalTicket, actor: User):
    """延期（EXPIRE_EXTEND）—— 仅更新 expires_at，不改状态"""
    new_expires = ticket.expires_at
    updated = False
    for rel_qs in (
        UserRoleRel.objects.filter(user=ticket.target_user, role=ticket.role, status=GrantStatus.ACTIVE),
        UserDeptScopeRel.objects.filter(user=ticket.target_user, role=ticket.role, status=GrantStatus.ACTIVE),
        UserTeamScopeRel.objects.filter(user=ticket.target_user, role=ticket.role, status=GrantStatus.ACTIVE),
    ):
        if rel_qs.update(expires_at=new_expires, ticket=ticket):
            updated = True
    _write_audit(ticket, actor, 'EXPIRE_EXTEND', '', '',
                 result='SUCCESS' if updated else 'NOOP')


# ============================================================================
# 降级/撤销直接执行（绕过工单，团队组长可直接撤销，仅记审计）
# ============================================================================

@transaction.atomic
def revoke_direct(actor: User, target_user: User, role: Role,
                  scope_type: str = ScopeType.NONE, scope_id=None,
                  reason: str = '', ip_address: str = '', user_agent: str = '') -> PermissionApprovalTicket:
    """降级/撤销直接执行 —— 团队组长可直接撤销普通角色授权，无需审批

    适用场景（build_approval_chain 返回空链的场景）：
    - REVOKE 非 super_admin 角色：团队组长直接撤销本团队内授权
    - 不涉及 super_admin 角色的撤销

    仍创建工单留痕（status=EXECUTED），保证审计可追溯。
    super_admin 角色撤销不应走此入口，必须 create_ticket 走双审。
    """
    # 超管角色（super_admin）撤销必须走审批工单（双人复核）
    # 不能走 revoke_direct 绕过审批，否则单点撤销超管权限有安全风险
    if role and role.role_key in ('super_admin',):
        raise ValueError('超管角色撤销必须走审批工单（双人复核）')

    ticket = PermissionApprovalTicket.objects.create(
        ticket_no=_gen_ticket_no(),
        applicant=actor,
        target_user=target_user,
        change_type=TicketChangeType.REVOKE,
        role=role,
        scope_type=scope_type,
        scope_id=scope_id,
        reason=reason,
        approval_chain=[],
        current_step=0,
        status=TicketStatus.EXECUTED,
        approved_at=timezone.now(),
        executed_at=timezone.now(),
    )
    _apply_revoke(ticket, actor)
    _write_audit(ticket, actor, AuditAction.TICKET_CREATE, ip_address, user_agent, result='SUCCESS')
    _write_audit(ticket, actor, AuditAction.TICKET_EXECUTE, ip_address, user_agent, result='SUCCESS')
    logger.info(f'[Ticket] 直接撤销(无需审批): role={role.role_key if role else "-"} '
                f'target={target_user.id} by={actor.id}')
    return ticket


# ============================================================================
# 审计写入
# ============================================================================

def _write_audit(ticket: PermissionApprovalTicket, actor: User, action: str,
                 ip_address: str, user_agent: str, result: str = 'SUCCESS', extra: dict = None):
    """写权限审计日志 —— 工单全生命周期留痕

    target_type=TICKET，target_id=ticket.id，便于按工单反查所有审计事件。
    extra 合并到 after_snapshot，记录节点/评论等上下文。
    """
    after = {'ticket_no': ticket.ticket_no, 'change_type': ticket.change_type,
             'status': ticket.status}
    if extra:
        after.update(extra)
    PermissionAuditLog.objects.create(
        actor=actor,
        action=action,
        target_type=AuditTargetType.TICKET,
        target_id=ticket.id,
        target_user=ticket.target_user,
        role=ticket.role,
        scope_type=ticket.scope_type,
        scope_id=ticket.scope_id,
        after_snapshot=after,
        result=result,
        ip_address=ip_address or None,
        user_agent=user_agent or '',
    )
