"""
apps.users.services.ticket_role - 角色配置变更工单服务（角色增删改 + 权限分配）

角色体系是 RBAC 核心，所有变更一律走工单：
- 新增/编辑角色、角色权限分配：普通风险，另一超管单审（共享审批池 + 回避原则）
- 删除角色：高风险，双超管复核（破坏性操作，审批期间可二次拦截）
- 审批链全部通过后由 _execute_role_change 落库，创建工单时只做预检不落库
- 角色变更工单审批人只能是超管（与申请人不同），创建时做超管配额校验，
  避免工单创建后无人可审卡死
"""
from django.utils import timezone
from loguru import logger

from apps.users.models import (
    TicketList, TicketRoleDetail, User, Role, RolePermissionRel, PermissionAuditLog,
    UserRoleRel, TicketStatus, TicketBizType, RoleOperation,
    RoleType, DataScope, AuditTargetType, GrantStatus,
)
from apps.users.services.ticket_base import (
    ApproverRole, AuditAction, _create_ticket_with_retry, _log_flow, _write_audit,
)
from apps.users.services.approval_chain import (
    _build_chain_node, _build_super_admin_chain_2step, _get_super_admin_ids,
)
from apps.users.services.rbac_service import assign_permissions_to_role


# 角色变更风险分级策略：
# - 普通（另一超管单审）：角色新增、编辑、权限分配
# - 高风险（双超管复核）：角色删除（破坏性操作，审批期间可二次拦截）
ROLE_RISK_LEVEL = {
    RoleOperation.ADD: 'normal',
    RoleOperation.EDIT: 'normal',
    RoleOperation.ASSIGN_PERMS: 'normal',
    RoleOperation.DELETE: 'high',
}


def _get_role_risk_level(operation: str) -> str:
    """根据角色操作类型返回风险等级

    返回 'normal' / 'high'，未匹配时默认 'normal'（走单审）。
    """
    return ROLE_RISK_LEVEL.get(operation, 'normal')


def _build_role_approval_chain(risk_level: str) -> list:
    """根据风险等级构建审批链 —— 角色变更审批人只能是超管

    - normal：单审（另一超管，共享审批池，回避原则排除申请人）
    - high：双超管复核（双人独立性由 _can_approve_for_role 保证）
    """
    if risk_level == 'high':
        return _build_super_admin_chain_2step()
    return [_build_chain_node(ApproverRole.SUPER_ADMIN)]


def _check_role_approver_quota(applicant: User, risk_level: str) -> None:
    """超管配额校验 —— 角色变更工单创建前置检查

    业务背景：角色变更工单的审批人是其他超管（回避原则排除申请人）。
    若可用超管不足（单审 <1 人 / 双审 <2 人），工单创建后无人可审，等于卡死。
    生产环境应硬约束超管数量，不足时拒绝创建并提示运维补人。
    """
    available_sa_ids = _get_super_admin_ids(exclude_user_id=applicant.id)
    need = 2 if risk_level == 'high' else 1
    if len(available_sa_ids) < need:
        logger.error(
            f'[RoleTicketQuota] 可用超管不足 {need} 人(当前 {len(available_sa_ids)} 人),'
            f'拒绝创建角色变更工单。申请人={applicant.username}。'
        )
        raise ValueError(
            f'可用超级管理员不足 {need} 人(当前 {len(available_sa_ids)} 人),'
            f'角色变更工单需另一超管审批，请先指派足够数量的超级管理员。'
        )


def create_role_ticket(
    actor: User,
    operation: str,
    target_role: Role = None,
    old_data: dict = None,
    new_data: dict = None,
    permission_ids: list = None,
    reason: str = '',
    ip_address: str = None,
    user_agent: str = None,
) -> TicketList:
    """创建角色配置变更工单（新增/编辑/删除/权限分配）

    一律走审批：增/改/权限分配 = 普通单审（另一超管），删 = 高风险双超管复核。
    审批链全部通过后由 _execute_role_change 落库，创建工单时只做预检不落库。

    返回创建的 TicketList 工单对象。
    """
    risk_level = _get_role_risk_level(operation)
    _check_role_approver_quota(actor, risk_level)
    approval_chain = _build_role_approval_chain(risk_level)

    # title 用于工单中心列表展示与模糊搜索，需包含"操作 + 对象名称"
    op_label = dict(RoleOperation.choices).get(operation, operation)
    if operation == RoleOperation.ASSIGN_PERMS:
        role_name = target_role.name if target_role else (new_data or {}).get('name', '')
        title = f'角色变更: 权限分配 {role_name}'.strip()
    else:
        name = (new_data or {}).get('name') or (old_data or {}).get('name') or ''
        title = f'角色变更: {op_label} {name}'.strip()

    def build(no):
        ticket = TicketList.objects.create(
            ticket_no=no,
            title=title,
            biz_type=TicketBizType.ROLE,
            status=TicketStatus.PENDING,
            risk_level=risk_level,
            applicant=actor,
            approval_chain=approval_chain,
            current_step=0,
            operation=operation,
        )
        TicketRoleDetail.objects.create(
            ticket=ticket,
            operation=operation,
            target_role=target_role,
            old_data=old_data,
            new_data=new_data,
            permission_ids=permission_ids or [],
            reason=reason,
        )
        _write_audit(ticket, actor, AuditAction.TICKET_CREATE, ip_address, user_agent)
        _log_flow(ticket, 'SUBMIT', actor=actor)
        return ticket

    # 唯一工单号并发冲突时自动重试（主表/详情/审计在同一 savepoint 内建，失败整体回滚）
    ticket = _create_ticket_with_retry(TicketBizType.ROLE, build)
    logger.info(f'[RoleTicket] 创建角色变更工单: {operation} '
                f'risk={risk_level} ticket={ticket.ticket_no} by={actor.id}')
    return ticket


def _execute_role_change(ticket: TicketList, actor: User):
    """执行角色变更 —— 审批链全部通过后由 _execute_grant_or_revoke 分发调用

    按工单详情中的 operation 路由到对应执行逻辑。
    执行失败抛 ValueError 回滚审批事务（工单留在 PENDING，审批人可处理后重试），
    避免"工单已执行但角色未变更"的幽灵状态。
    """
    detail = getattr(ticket, 'role_detail', None)
    if not detail:
        raise ValueError(f'工单 {ticket.ticket_no} 缺少 role_detail，无法执行')

    operation = detail.operation
    if operation == RoleOperation.ADD:
        _apply_role_add(ticket, actor)
    elif operation == RoleOperation.EDIT:
        _apply_role_edit(ticket, actor)
    elif operation == RoleOperation.DELETE:
        _apply_role_delete(ticket, actor)
    elif operation == RoleOperation.ASSIGN_PERMS:
        _apply_role_assign_perms(ticket, actor)
    else:
        raise ValueError(f'未知的角色操作类型: {operation}')


def _write_role_audit(ticket: TicketList, actor: User, action: str,
                      before: dict = None, after: dict = None):
    """写角色变更审计 —— target_type=ROLE 定位到角色本身

    与工单流程审计（TICKET_*）互补：工单审计记录流程流转，角色审计记录
    角色实体本身的变更快照（before/after），便于按角色反查变更历史。
    审计可丢、业务不可丢：写入失败仅记日志，绝不向上抛异常。
    """
    detail = getattr(ticket, 'role_detail', None)
    role = detail.target_role if detail else None
    try:
        PermissionAuditLog.objects.create(
            actor=actor,
            action=action,
            target_type=AuditTargetType.ROLE,
            target_id=role.id if role else None,
            role=role,
            before_snapshot=before,
            after_snapshot=after,
            result='SUCCESS',
        )
    except Exception as exc:
        logger.error(
            f'[Audit] _write_role_audit failed (不阻断主业务): action={action} '
            f'ticket_no={ticket.ticket_no} actor={getattr(actor, "id", None)} err={exc}'
        )


def _apply_role_add(ticket: TicketList, actor: User):
    """新增角色执行 —— 含软删同名角色恢复语义

    审批期间角色编码可能被其他工单占用，执行时二次校验；
    同名软删角色恢复而非新建（保住 RolePermissionRel 等关联记录的身份，
    避免历史绑定关系孤儿化）。
    """
    from django.db import IntegrityError

    new_data = ticket.role_detail.new_data or {}
    code = (new_data.get('code') or '').strip()
    name = (new_data.get('name') or '').strip()
    if not code or not name:
        raise ValueError('角色编码与名称不能为空')
    if Role.objects.filter(role_key=code, is_deleted=False).exists():
        raise ValueError(f'角色编码已存在: {code}')

    deleted_role = Role.objects.filter(role_key=code, is_deleted=True).first()
    if deleted_role:
        deleted_role.is_deleted = False
        deleted_role.deleted_at = None
        deleted_role.name = name
        deleted_role.description = new_data.get('description') or ''
        try:
            deleted_role.save()
        except IntegrityError:
            raise ValueError(f'角色编码已存在: {code}')
        logger.info(f'[RoleTicket] 恢复软删角色: {deleted_role.role_key} '
                    f'(ticket={ticket.ticket_no}, role_id={deleted_role.id})')
        _write_role_audit(ticket, actor, 'ROLE_CREATE',
                          before=None,
                          after={'id': deleted_role.id, 'role_key': code, 'name': name,
                                 'restored': True})
        return

    role = Role.objects.create(
        role_key=code,
        name=name,
        description=new_data.get('description') or '',
        role_type=new_data.get('role_type') or RoleType.NORMAL_USER,
        data_scope=new_data.get('data_scope') or DataScope.TEAM,
        is_builtin=False,
    )
    logger.info(f'[RoleTicket] 新增角色: {code} (ticket={ticket.ticket_no}, role_id={role.id})')
    _write_role_audit(ticket, actor, 'ROLE_CREATE',
                      before=None,
                      after={'id': role.id, 'role_key': code, 'name': name,
                             'description': role.description})


def _apply_role_edit(ticket: TicketList, actor: User):
    """编辑角色执行 —— 名称/编码/描述，执行时二次校验唯一性

    内置角色编码不可修改（与视图校验一致，防止权限判定失效）；
    审批期间编码可能被其他工单占用，执行时二次校验。
    """
    role = ticket.role_detail.target_role
    if not role:
        raise ValueError('目标角色不存在或已被删除，无法执行编辑')

    new_data = ticket.role_detail.new_data or {}
    old_data = ticket.role_detail.old_data or {}
    name = (new_data.get('name') or '').strip()
    code = (new_data.get('code') or '').strip()
    desc = new_data.get('description')
    if code and code != role.role_key:
        if role.is_builtin:
            raise ValueError('内置角色编码不可修改')
        if Role.objects.filter(role_key=code, is_deleted=False).exclude(id=role.id).exists():
            raise ValueError(f'角色编码已存在: {code}')
        role.role_key = code
    if name:
        role.name = name
    if desc is not None:
        role.description = desc
    role.save()
    logger.info(f'[RoleTicket] 编辑角色: id={role.id} (ticket={ticket.ticket_no})')
    _write_role_audit(ticket, actor, 'ROLE_UPDATE',
                      before=old_data or {'id': role.id, 'role_key': role.role_key},
                      after={'id': role.id, 'role_key': role.role_key,
                             'name': role.name, 'description': role.description})


def _apply_role_delete(ticket: TicketList, actor: User):
    """删除角色执行（软删）—— 执行时二次校验用户绑定数

    审批通过到执行之间角色可能被授予用户，二次校验拦截并回滚事务。
    目标角色已被其他工单软删时视为幂等成功（目标状态已达成）。
    """
    role = ticket.role_detail.target_role
    if not role:
        logger.warning(f'[RoleTicket] 删除角色时目标已不存在(幂等跳过): id='
                       f'{ticket.role_detail.target_role_id} (ticket={ticket.ticket_no})')
        return
    if role.is_deleted:
        logger.warning(f'[RoleTicket] 删除角色时目标已软删(幂等跳过): {role.role_key} '
                       f'(ticket={ticket.ticket_no})')
        return
    if role.is_builtin:
        raise ValueError('内置角色不可删除')
    user_count = UserRoleRel.objects.filter(role=role, status=GrantStatus.ACTIVE).count()
    if user_count > 0:
        raise ValueError(f'该角色被 {user_count} 个用户使用，请先解除用户关联')
    role.is_deleted = True
    role.deleted_at = timezone.now()
    role.save()
    logger.info(f'[RoleTicket] 删除角色: {role.role_key} '
                f'(ticket={ticket.ticket_no}, role_id={role.id})')
    _write_role_audit(ticket, actor, 'ROLE_DELETE',
                      before={'id': role.id, 'role_key': role.role_key, 'name': role.name},
                      after={'id': role.id, 'role_key': role.role_key, 'deleted': True})


def _apply_role_assign_perms(ticket: TicketList, actor: User):
    """角色权限分配执行 —— 全量覆盖角色权限（复用 assign_permissions_to_role）

    审批期间权限点可能被删除，assign_permissions_to_role 内部会过滤无效 ID。
    """
    role = ticket.role_detail.target_role
    if not role:
        raise ValueError('目标角色不存在或已被删除，无法执行权限分配')
    perm_ids = ticket.role_detail.permission_ids or []
    valid_ids, invalid_count = assign_permissions_to_role(role, perm_ids, actor)
    logger.info(f'[RoleTicket] 角色权限分配: role={role.role_key} '
                f'valid={len(valid_ids)} invalid={invalid_count} (ticket={ticket.ticket_no})')
    _write_role_audit(ticket, actor, 'ROLE_PERMS_ASSIGN',
                      before={'role_key': role.role_key},
                      after={'role_key': role.role_key, 'permission_ids': valid_ids,
                             'invalid_count': invalid_count})
