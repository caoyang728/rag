"""
apps.users.services.ticket_org - 组织变更工单服务（部门/团队增删改）
"""
from django.db import transaction
from loguru import logger

from apps.users.models import (
    TicketList, TicketOrgDetail, User, Department, Team,
    TicketStatus, TicketBizType, OrgChangeType, OrgOperation,
)
from apps.users.services.ticket_base import ApproverRole, AuditAction, _gen_ticket_no, _log_flow, _write_audit
from apps.users.services.approval_chain import _build_chain_node, _build_user_admin_then_super_chain


# 组织变更风险分级策略：
# - 普通（单审 USER_ADMIN）：部门/团队新增、编辑（结构性变更但可逆）
# - 高风险（双审 USER_ADMIN + SUPER_ADMIN）：部门/团队删除（破坏性操作，审批期间可二次拦截）
ORG_RISK_LEVEL = {
    (OrgChangeType.DEPT, OrgOperation.ADD): 'normal',
    (OrgChangeType.DEPT, OrgOperation.EDIT): 'normal',
    (OrgChangeType.DEPT, OrgOperation.DELETE): 'high',
    (OrgChangeType.TEAM, OrgOperation.ADD): 'normal',
    (OrgChangeType.TEAM, OrgOperation.EDIT): 'normal',
    (OrgChangeType.TEAM, OrgOperation.DELETE): 'high',
}


def _get_org_risk_level(org_type: str, operation: str) -> str:
    """根据组织类型和操作返回风险等级

    返回 'normal' / 'high'，未匹配时默认 'normal'（走单审）。
    """
    return ORG_RISK_LEVEL.get((org_type, operation), 'normal')


def _build_org_approval_chain(risk_level: str) -> list:
    """根据风险等级构建审批链

    - normal：单审（USER_ADMIN 用户管理员）
    - high：双审（USER_ADMIN 审核 + SUPER_ADMIN 复核，双人独立性由 _can_approve_for_role 保证）
    """
    if risk_level == 'high':
        return _build_user_admin_then_super_chain()
    return [_build_chain_node(ApproverRole.USER_ADMIN)]


def create_org_ticket(
    actor: User,
    org_type: str,
    operation: str,
    target_data: dict,
    reason: str,
    old_data: dict = None,
    new_data: dict = None,
    ip_address: str = None,
    user_agent: str = None,
) -> TicketList:
    """创建组织架构变更工单（部门/团队新增、编辑、删除）

    组织变更一律走审批：增/改 = 普通单审（USER_ADMIN），删 = 高风险双审
    （USER_ADMIN + SUPER_ADMIN）。审批链全部通过后由 _execute_org_change
    落库生效，创建工单时只做预检不落库。

    返回创建的 TicketList 工单对象。
    """
    risk_level = _get_org_risk_level(org_type, operation)
    approval_chain = _build_org_approval_chain(risk_level)

    # title 用于工单中心列表展示与模糊搜索，需包含"操作 + 对象 + 名称"
    name = (target_data or {}).get('name') or (new_data or {}).get('name') or ''
    type_label = dict(OrgChangeType.choices).get(org_type, org_type)
    op_label = dict(OrgOperation.choices).get(operation, operation)
    title = f'组织变更: {op_label}{type_label} {name}'.strip()

    ticket = TicketList.objects.create(
        ticket_no=_gen_ticket_no(TicketBizType.ORG),
        title=title,
        biz_type=TicketBizType.ORG,
        status=TicketStatus.PENDING,
        risk_level=risk_level,
        applicant=actor,
        approval_chain=approval_chain,
        current_step=0,
        operation=operation,
    )
    TicketOrgDetail.objects.create(
        ticket=ticket,
        org_type=org_type,
        operation=operation,
        target_data=target_data,
        old_data=old_data,
        new_data=new_data,
        reason=reason,
    )
    _write_audit(ticket, actor, AuditAction.TICKET_CREATE, ip_address, user_agent)
    _log_flow(ticket, 'SUBMIT', actor=actor)
    logger.info(f'[OrgTicket] 创建组织变更工单: {org_type}:{operation} '
                f'risk={risk_level} ticket={ticket.ticket_no} by={actor.id}')
    return ticket


@transaction.atomic
def _execute_org_change(ticket: TicketList, actor: User):
    """执行组织变更 —— 审批链全部通过后由 _execute_grant_or_revoke 分发调用

    按工单详情中的 org_type + operation 路由到对应执行逻辑。
    执行失败抛 ValueError 回滚审批事务（工单留在 PENDING，审批人可处理后重试），
    避免"工单已执行但组织未变更"的幽灵状态。

    Department/Team 的 post_save 信号会自动同步知识节点树（node_sync），
    执行层只需保存模型即可。
    """
    detail = getattr(ticket, 'org_detail', None)
    if not detail:
        raise ValueError(f'工单 {ticket.ticket_no} 缺少 org_detail，无法执行')

    org_type = detail.org_type
    operation = detail.operation
    target = detail.target_data or {}
    new_data = detail.new_data or {}

    if org_type == OrgChangeType.DEPT:
        _execute_dept_change(operation, target, new_data, ticket)
    elif org_type == OrgChangeType.TEAM:
        _execute_team_change(operation, target, new_data, ticket)
    else:
        raise ValueError(f'未知的组织类型: {org_type}')


def _execute_dept_change(operation: str, target: dict, new_data: dict, ticket: TicketList):
    """执行部门变更 —— 按 operation 路由"""
    if operation == OrgOperation.ADD:
        _create_dept(target, new_data, ticket)
    elif operation == OrgOperation.EDIT:
        _update_dept(target, new_data, ticket)
    elif operation == OrgOperation.DELETE:
        _delete_dept(target, ticket)
    else:
        raise ValueError(f'未知的部门操作类型: {operation}')


def _create_dept(target: dict, new_data: dict, ticket: TicketList):
    """创建部门 —— 含软删同名行恢复语义

    审批期间名称/编码可能被其他工单占用，执行时二次校验；
    同名软删行恢复而非新建（保住 KnowledgeNode 的 ref_id 身份，避免旧节点孤儿化）。
    """
    from django.db import IntegrityError
    from apps.users.services.org_service import _auto_code, _ensure_unique_code

    name = (new_data.get('name') or target.get('name') or '').strip()
    if not name:
        raise ValueError('部门名称不能为空')
    code = (new_data.get('code') or '').strip()
    if not code:
        code = _auto_code(name)
    code = _ensure_unique_code(code, Department)

    if Department.objects.filter(name=name, is_deleted=False).exists():
        raise ValueError(f'部门“{name}”已存在')

    deleted_dept = Department.objects.filter(name=name, is_deleted=True).first()
    if deleted_dept:
        restored_code = code
        if Department.objects.filter(code=restored_code, is_deleted=False).exclude(id=deleted_dept.id).exists():
            restored_code = _ensure_unique_code(restored_code, Department)
        deleted_dept.is_deleted = False
        deleted_dept.name = name
        deleted_dept.code = restored_code
        try:
            deleted_dept.save()
        except IntegrityError:
            raise ValueError(f'部门“{name}”已存在')
        logger.info(f'[OrgTicket] 恢复软删部门: {deleted_dept.name} '
                    f'(ticket={ticket.ticket_no}, dept_id={deleted_dept.id})')
        return

    try:
        Department.objects.create(name=name, code=code)
    except IntegrityError:
        raise ValueError(f'部门“{name}”已存在')
    logger.info(f'[OrgTicket] 创建部门: {name} (ticket={ticket.ticket_no})')


def _update_dept(target: dict, new_data: dict, ticket: TicketList):
    """编辑部门 —— 名称/编码，执行时二次校验唯一性"""
    dept = Department.objects.filter(id=target.get('id'), is_deleted=False).first()
    if not dept:
        raise ValueError('部门不存在或已被删除，无法执行编辑')

    name = (new_data.get('name') or '').strip()
    code = (new_data.get('code') or '').strip()
    if name:
        if Department.objects.filter(name=name, is_deleted=False).exclude(id=dept.id).exists():
            raise ValueError(f'部门“{name}”已存在')
        dept.name = name
    if code:
        if Department.objects.filter(code=code, is_deleted=False).exclude(id=dept.id).exists():
            raise ValueError(f'部门编码冲突: {code}')
        dept.code = code
    dept.save()
    logger.info(f'[OrgTicket] 编辑部门: id={dept.id} (ticket={ticket.ticket_no})')


def _delete_dept(target: dict, ticket: TicketList):
    """删除部门（软删）—— 执行时二次校验用户/团队数

    审批通过到执行之间部门下可能新增用户/团队，二次校验拦截并回滚事务。
    目标部门已被其他工单软删时视为幂等成功（目标状态已达成）。
    """
    dept = Department.objects.filter(id=target.get('id'), is_deleted=False).first()
    if not dept:
        logger.warning(f'[OrgTicket] 删除部门时目标已不存在(幂等跳过): id={target.get("id")} '
                       f'(ticket={ticket.ticket_no})')
        return
    user_count = User.objects.filter(department=dept, is_deleted=False).count()
    if user_count > 0:
        raise ValueError(f'该部门下还有 {user_count} 个用户，无法删除')
    team_count = Team.objects.filter(department=dept, is_deleted=False).count()
    if team_count > 0:
        raise ValueError(f'该部门下还有 {team_count} 个团队，请先删除或迁移团队')
    dept.is_deleted = True
    dept.save()
    logger.info(f'[OrgTicket] 删除部门: {dept.name} (ticket={ticket.ticket_no}, dept_id={dept.id})')


def _execute_team_change(operation: str, target: dict, new_data: dict, ticket: TicketList):
    """执行团队变更 —— 按 operation 路由"""
    if operation == OrgOperation.ADD:
        _create_team(target, new_data, ticket)
    elif operation == OrgOperation.EDIT:
        _update_team(target, new_data, ticket)
    elif operation == OrgOperation.DELETE:
        _delete_team(target, ticket)
    else:
        raise ValueError(f'未知的团队操作类型: {operation}')


def _create_team(target: dict, new_data: dict, ticket: TicketList):
    """创建团队 —— 含软删同名行恢复语义（同部门维度），执行时二次校验"""
    from django.db import IntegrityError
    from apps.users.services.org_service import _auto_code, _ensure_unique_code

    name = (new_data.get('name') or target.get('name') or '').strip()
    if not name:
        raise ValueError('团队名称不能为空')
    dept_id = new_data.get('department_id') or target.get('department_id')
    if not dept_id:
        raise ValueError('部门ID不能为空')
    dept_id = int(dept_id)
    dept = Department.objects.filter(id=dept_id, is_deleted=False).first()
    if not dept:
        raise ValueError('指定的部门不存在')

    if Team.objects.filter(name=name, department_id=dept_id, is_deleted=False).exists():
        raise ValueError(f'部门“{dept.name}”下已存在团队“{name}”')

    code = (new_data.get('code') or '').strip()
    if not code:
        prefix = dept.code or _auto_code(dept.name)
        code = _auto_code(name, prefix)
    code = _ensure_unique_code(code, Team)

    deleted_team = Team.objects.filter(name=name, department_id=dept_id, is_deleted=True).first()
    if deleted_team:
        restored_code = code
        if Team.objects.filter(code=restored_code, is_deleted=False).exclude(id=deleted_team.id).exists():
            restored_code = _ensure_unique_code(restored_code, Team)
        deleted_team.is_deleted = False
        deleted_team.name = name
        deleted_team.code = restored_code
        deleted_team.description = new_data.get('description') or ''
        deleted_team.department_id = dept_id
        try:
            deleted_team.save()
        except IntegrityError:
            raise ValueError(f'部门“{dept.name}”下已存在团队“{name}”')
        logger.info(f'[OrgTicket] 恢复软删团队: {deleted_team.name} '
                    f'(ticket={ticket.ticket_no}, team_id={deleted_team.id})')
        return

    try:
        Team.objects.create(
            name=name,
            code=code,
            department_id=dept_id,
            description=new_data.get('description'),
        )
    except IntegrityError:
        raise ValueError(f'部门“{dept.name}”下已存在团队“{name}”')
    logger.info(f'[OrgTicket] 创建团队: {name} (ticket={ticket.ticket_no})')


def _update_team(target: dict, new_data: dict, ticket: TicketList):
    """编辑团队 —— 名称/编码/描述，支持跨部门迁移，执行时二次校验唯一性"""
    team = Team.objects.filter(id=target.get('id'), is_deleted=False).first()
    if not team:
        raise ValueError('团队不存在或已被删除，无法执行编辑')

    name = (new_data.get('name') or '').strip()
    code = (new_data.get('code') or '').strip()
    desc = new_data.get('description')
    dept_id = new_data.get('department_id')
    if dept_id:
        dept_id = int(dept_id)
        if not Department.objects.filter(id=dept_id, is_deleted=False).exists():
            raise ValueError('指定的部门不存在')
        team.department_id = dept_id
    if name:
        if Team.objects.filter(name=name, department_id=team.department_id,
                               is_deleted=False).exclude(id=team.id).exists():
            raise ValueError(f'部门下已存在团队“{name}”')
        team.name = name
    if code:
        if Team.objects.filter(code=code, is_deleted=False).exclude(id=team.id).exists():
            raise ValueError(f'团队编码冲突: {code}')
        team.code = code
    if desc is not None:
        team.description = desc
    team.save()
    logger.info(f'[OrgTicket] 编辑团队: id={team.id} (ticket={ticket.ticket_no})')


def _delete_team(target: dict, ticket: TicketList):
    """删除团队（软删）—— 执行时二次校验成员数/文档数

    与 TeamViewSet.destroy 校验一致：成员数 + 团队节点及子孙分类节点下的文档数。
    目标团队已被其他工单软删时视为幂等成功。
    """
    team = Team.objects.filter(id=target.get('id'), is_deleted=False).first()
    if not team:
        logger.warning(f'[OrgTicket] 删除团队时目标已不存在(幂等跳过): id={target.get("id")} '
                       f'(ticket={ticket.ticket_no})')
        return
    user_count = User.objects.filter(team=team, is_deleted=False).count()
    if user_count > 0:
        raise ValueError(f'该团队下还有 {user_count} 个成员，无法删除')

    from apps.knowledge.models import KnowledgeNode
    from apps.knowledge.node_sync import count_docs_in_subtree
    team_node = KnowledgeNode.objects.filter(
        node_level=3, ref_id=team.id, is_deleted=False
    ).first()
    if team_node:
        doc_count = count_docs_in_subtree(team_node.id)
        if doc_count > 0:
            raise ValueError(f'该团队下有 {doc_count} 个文档，请先迁移或删除后再操作')

    team.is_deleted = True
    team.save()
    logger.info(f'[OrgTicket] 删除团队: {team.name} (ticket={ticket.ticket_no}, team_id={team.id})')
