"""
apps.users.services.ticket_permission - 权限配置工单的创建、流转与授权执行

工单创建（create_ticket）、审批流转（approve/reject/cancel）以及审批通过后
的授权写入（_apply_grant / _apply_revoke / _apply_extend / _apply_role_change）。
安全（security）与组织（org）工单的执行分发也经由本模块的 _execute_grant_or_revoke。
"""
from django.db import transaction
from django.utils import timezone
from loguru import logger

from apps.users.models import (
    TicketList, TicketPermissionDetail, UserRoleRel, UserDeptScopeRel, UserTeamScopeRel,
    Role, User, Department, Team,
    TicketStatus, TicketChangeType, ScopeType, GrantStatus, AuditTargetType, TicketBizType,
)
from apps.users.services.ticket_base import (
    ApproveStepStatus, AuditAction, TEAM_ROLE_KEYS,
    _create_ticket_with_retry, _log_flow, _write_audit,
)
from apps.users.services.approval_chain import (
    _can_approve_for_role, _check_sod_conflict, _check_super_admin_quota,
    _detect_team_role_in_service, _detect_dept_role_in_service, build_approval_chain,
)
from apps.users.services.ticket_security import _execute_security_change
from apps.users.services.ticket_org import _execute_org_change
from apps.users.services.ticket_role import _execute_role_change


# ============================================================================
# 工单创建与流转
# ============================================================================

def _create_permission_ticket(applicant, target_user, change_type: str,
                              role: Role, previous_role: Role, scope_type: str,
                              scope_id, effective_from, expires_at, reason: str,
                              chain: list, status: str,
                              approved_at=None, executed_at=None) -> TicketList:
    """创建统一工单（主表 + 权限详情子表 + 提交流转日志）—— 原子操作

    主表承接流程字段（工单号/状态/审批链/时间），业务字段入 TicketPermissionDetail，
    提交动作写一条 SUBMIT 流转日志。title 供工单中心列表展示与模糊搜索。
    """
    role_key = role.role_key if role else ''

    def build(no):
        ticket = TicketList.objects.create(
            ticket_no=no,
            title=f'权限·{change_type} {role_key}'.strip(),
            biz_type=TicketBizType.PERMISSION,
            status=status,
            risk_level='normal',
            applicant=applicant,
            approval_chain=chain,
            current_step=0,
            approved_at=approved_at,
            executed_at=executed_at,
        )
        TicketPermissionDetail.objects.create(
            ticket=ticket,
            target_user=target_user,
            change_type=change_type,
            role=role,
            previous_role=previous_role,
            scope_type=scope_type,
            scope_id=scope_id,
            effective_from=effective_from,
            expires_at=expires_at,
            reason=reason,
        )
        _log_flow(ticket, 'SUBMIT', actor=applicant)
        return ticket

    # 唯一工单号并发冲突时自动重试（主表/详情/日志在同一 savepoint 内建，失败整体回滚）
    return _create_ticket_with_retry(TicketBizType.PERMISSION, build)


@transaction.atomic
def create_ticket(applicant, target_user, change_type: str,
                  role: Role, scope_type: str = ScopeType.NONE, scope_id=None,
                  effective_from=None, expires_at=None, reason: str = '',
                  ip_address: str = '', user_agent: str = '',
                  previous_role: Role = None) -> TicketList:
    """创建审批工单 —— 授权变更统一入口

    流程:
    1. 入口校验:SoD 互斥检查(4 高权 4 选 1) + 超管硬约束(可用超管 ≥2)
    2. 构造审批链(build_approval_chain)
    3. 空链 → REVOKE 普通角色:直接执行撤销并返回已执行工单(记审计)
    4. 非空链 → 创建 PENDING 工单,等待逐级审批
    5. 写 TICKET_CREATE 审计

    返回:工单对象(已执行或待审批)

    :param previous_role:仅 ROLE_CHANGE 使用,记录变更前旧角色(执行时撤销目标)
    """
    # ── 入口校验 1:SoD 互斥(仅 GRANT / ROLE_CHANGE 需要校验,REVOKE 是减少角色不冲突) ──
    if change_type in (TicketChangeType.GRANT, TicketChangeType.ROLE_CHANGE) and role:
        _check_sod_conflict(target_user, role)

    # ── 入口校验 2:双超管硬约束(super_admin / user_admin 工单需双超管审批,可用超管 <2 直接拒绝) ──
    # user_admin 走双超管链(与 super_admin 同),配额不足时工单会卡死,故入口拒绝
    if role and role.role_key in ('super_admin', 'user_admin'):
        _check_super_admin_quota(applicant)

    # ── 入口校验 3:团队级互斥自动转 ROLE_CHANGE ──
    # 业务规则:同团队/同部门内团队角色(viewer/contributor/team_leader)互斥,
    # 高等级覆盖低等级。申请同 scope 新角色时,若已有旧角色,自动转为 ROLE_CHANGE
    # (原子撤销旧角色 + 授予新角色),避免同 scope 出现多条 ACTIVE 记录违反 DB 唯一约束。
    # 此校验下沉到 create_ticket,确保所有工单创建路径统一拦截。
    if (change_type == TicketChangeType.GRANT
            and role and role.role_key in TEAM_ROLE_KEYS
            and scope_type in (ScopeType.TEAM, ScopeType.DEPT) and scope_id):
        if scope_type == ScopeType.TEAM:
            existing_role = _detect_team_role_in_service(target_user, scope_id)
        else:
            existing_role = _detect_dept_role_in_service(target_user, scope_id)
        if existing_role and existing_role.id != role.id:
            previous_role = existing_role
            change_type = TicketChangeType.ROLE_CHANGE
            logger.info(
                f'[Ticket] 团队级互斥自动转 ROLE_CHANGE: '
                f'user={target_user.username} scope={scope_type}#{scope_id} '
                f'{existing_role.role_key} -> {role.role_key}'
            )

    # ── 入口校验 4:工单防重 —— 同目标用户同 scope 已有待审批的同类角色工单则拒绝 ──
    # 业务背景:重复提交相同授权申请会堆积 PENDING 工单,审批人处理一单后其余变废单,
    # 且高等级覆盖低等级语义下同 scope 不应并存多个待批角色。仅拦截待审批(非已执行/已驳回)。
    if change_type in (TicketChangeType.GRANT, TicketChangeType.ROLE_CHANGE) and role:
        dup_qs = TicketList.objects.filter(
            biz_type=TicketBizType.PERMISSION,
            status=TicketStatus.PENDING,
            permission_detail__target_user=target_user,
            permission_detail__scope_type=scope_type,
            permission_detail__scope_id=scope_id,
        )
        if scope_type in (ScopeType.TEAM, ScopeType.DEPT):
            # 团队/部门 scope:同范围任意团队角色互斥,存在任一 PENDING 即拒绝
            dup_qs = dup_qs.filter(permission_detail__role__role_key__in=TEAM_ROLE_KEYS)
        else:
            # 全局 scope:同角色 PENDING 才拒绝
            dup_qs = dup_qs.filter(permission_detail__role=role)
        if dup_qs.exists():
            raise ValueError('该用户在此范围内已有待审批的授权工单，请勿重复提交')

    # ── 入口校验 5:管理岗名额唯一 —— 部门已有经理 / 团队已有组长时不可重复任命 ──
    # 业务背景:Team.leader_id / Department.leader_id 与 team_leader / dept_manager 授权
    # 一一对应(见 _sync_leader_for_role)。已有现任者时,仅允许现任者本人续期/变更;
    # 任命新人必须先撤销现任(REVOKE 工单)再任命,避免出现双组长/双经理。
    if (change_type in (TicketChangeType.GRANT, TicketChangeType.ROLE_CHANGE)
            and role and role.role_key in ('team_leader', 'dept_manager') and scope_id):
        existing_leader_id = None
        if role.role_key == 'team_leader' and scope_type == ScopeType.TEAM:
            # 团队现任组长:优先 leader_id 字段,兜底活跃 team_leader 授权(字段与授权双来源对齐)
            team = Team.objects.filter(id=scope_id, is_deleted=False).only('leader_id').first()
            if team and team.leader_id:
                existing_leader_id = team.leader_id
            if not existing_leader_id:
                existing_leader_id = UserTeamScopeRel.objects.filter(
                    team_id=scope_id, role__role_key='team_leader',
                    status=GrantStatus.ACTIVE,
                ).values_list('user_id', flat=True).first()
        elif role.role_key == 'dept_manager' and scope_type == ScopeType.DEPT:
            # 部门现任经理:优先 leader_id 字段,兜底活跃 dept_manager 授权
            dept = Department.objects.filter(id=scope_id, is_deleted=False).only('leader_id').first()
            if dept and dept.leader_id:
                existing_leader_id = dept.leader_id
            if not existing_leader_id:
                existing_leader_id = UserDeptScopeRel.objects.filter(
                    dept_id=scope_id, role__role_key='dept_manager',
                    status=GrantStatus.ACTIVE,
                ).values_list('user_id', flat=True).first()
        # 已有现任者且非本人 → 拒绝(换人需先撤销现任);本人续期/变更放行
        if existing_leader_id and existing_leader_id != target_user.id:
            scope_label = '团队' if role.role_key == 'team_leader' else '部门'
            leader_label = '组长' if role.role_key == 'team_leader' else '经理'
            raise ValueError(
                f'该{scope_label}已有{leader_label},如需更换请先撤销现任{leader_label}后再任命'
            )

    chain = build_approval_chain(applicant, target_user, change_type,
                                  role, scope_type, scope_id,
                                  previous_role=previous_role)

    # 空审批链:降级/撤销低权角色 → 直接执行(viewer 跨团队撤销/contributor 撤销)
    if not chain:
        now = timezone.now()
        ticket = _create_permission_ticket(
            applicant, target_user, change_type, role, previous_role,
            scope_type, scope_id, effective_from, expires_at, reason,
            chain=[], status=TicketStatus.EXECUTED,
            approved_at=now, executed_at=now,
        )
        _execute_grant_or_revoke(ticket, actor=applicant)
        _write_audit(ticket, applicant, AuditAction.TICKET_CREATE,
                     ip_address, user_agent, result='SUCCESS')
        _write_audit(ticket, applicant, AuditAction.TICKET_EXECUTE,
                     ip_address, user_agent, result='SUCCESS')
        logger.info(f'[Ticket] 直接执行(无审批链): {change_type} '
                    f'{role.role_key if role else "-"} -> {target_user.id}')
        return ticket

    # 非空审批链:创建待审批工单
    ticket = _create_permission_ticket(
        applicant, target_user, change_type, role, previous_role,
        scope_type, scope_id, effective_from, expires_at, reason,
        chain=chain, status=TicketStatus.PENDING,
    )
    _write_audit(ticket, applicant, AuditAction.TICKET_CREATE,
                 ip_address, user_agent, result='SUCCESS')
    logger.info(f'[Ticket] 创建待审批工单: {ticket.ticket_no} '
                f'approvers={[n.get("approver_role") for n in chain]}')
    return ticket


@transaction.atomic
def approve_ticket(ticket: TicketList, approver: User,
                   comment: str = '', ip_address: str = '', user_agent: str = '') -> TicketList:
    """审批通过当前节点 —— 共享审批池模式：任一符合 approver_role 的用户均可审批，先到先得

    校验（共享审批池 + 先到先得）：
    - 工单必须 PENDING
    - approver 必须具备当前节点 approver_role 所要求的角色/身份
    - 审批时回填 approver_id（锁定审批人，防止并发审批）
    - 不允许跨节点审批
    - select_for_update 防并发：两个管理员同时审批时只有一个能成功

    末节点通过 → status=APPROVED → 同步执行授权写入 → status=EXECUTED
    每步流转写 TicketFlowLog（APPROVE / EXECUTE），详情页时间线渲染用。
    """
    # select_for_update 防止并发审批：同一工单同时只能被一个事务修改
    ticket = TicketList.objects.select_for_update().get(pk=ticket.pk)

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
        _log_flow(ticket, 'APPROVE', actor=approver, comment=comment, step=ticket.current_step)
        _execute_grant_or_revoke(ticket, actor=approver)
        ticket.status = TicketStatus.EXECUTED
        ticket.executed_at = timezone.now()
        ticket.save()
        _log_flow(ticket, 'EXECUTE', actor=approver, step=ticket.current_step)
        _write_audit(ticket, approver, AuditAction.TICKET_EXECUTE,
                     ip_address, user_agent, result='SUCCESS')
        logger.info(f'[Ticket] 工单审批通过并执行: {ticket.ticket_no} '
                    f'approver={approver.id} role={approver_role}')
    else:
        # 推进到下一节点
        _log_flow(ticket, 'APPROVE', actor=approver, comment=comment, step=ticket.current_step)
        ticket.current_step += 1
        ticket.save()
        logger.info(f'[Ticket] 工单节点通过，推进下一节点: {ticket.ticket_no} step={ticket.current_step}')
    return ticket


@transaction.atomic
def reject_ticket(ticket: TicketList, rejector: User,
                  comment: str = '', ip_address: str = '', user_agent: str = '') -> TicketList:
    """驳回工单 —— 共享审批池模式：任一符合 approver_role 的用户均可驳回

    驳回人可以是当前节点审批人（角色匹配），或 super_admin（兜底越级驳回）。
    驳回动作写 TicketFlowLog（REJECT），工单终态 REJECTED。
    """
    ticket = TicketList.objects.select_for_update().get(pk=ticket.pk)

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
    _log_flow(ticket, 'REJECT', actor=rejector, comment=comment, step=ticket.current_step)
    _write_audit(ticket, rejector, AuditAction.TICKET_REJECT,
                 ip_address, user_agent, result='SUCCESS',
                 extra={'comment': comment})
    logger.info(f'[Ticket] 工单被驳回: {ticket.ticket_no} by={rejector.id}')
    return ticket


@transaction.atomic
def cancel_ticket(ticket: TicketList, actor: User,
                  ip_address: str = '', user_agent: str = '') -> TicketList:
    """发起人撤回工单 —— 仅 PENDING 状态可撤回，已执行不可撤

    防止授权已生效后撤回工单造成状态不一致。撤回动作写 TicketFlowLog（CANCEL）。
    """
    if ticket.status != TicketStatus.PENDING:
        raise ValueError('仅待审批工单可撤回')
    if ticket.applicant_id != actor.id and not actor.is_super_admin:
        raise PermissionError('仅发起人可撤回工单')

    ticket.status = TicketStatus.CANCELLED
    ticket.save()
    _log_flow(ticket, 'CANCEL', actor=actor, step=ticket.current_step)
    _write_audit(ticket, actor, AuditAction.TICKET_CANCEL,
                 ip_address, user_agent, result='SUCCESS')
    logger.info(f'[Ticket] 工单撤回: {ticket.ticket_no} by={actor.id}')
    return ticket


# ============================================================================
# 工单执行：审批通过后写入授权表（GRANT）或撤销授权（REVOKE）
# ============================================================================

def _execute_grant_or_revoke(ticket: TicketList, actor: User):
    """执行工单生效逻辑 —— 工单 APPROVED 后按 biz_type 分发

    - permission:按 change_type 执行授权写入(GRANT/REVOKE/SCOPE_CHANGE/EXPIRE_EXTEND/ROLE_CHANGE)
    - org:执行部门/团队增删改(_execute_org_change)
    - security:执行安全配置变更(_execute_security_change)
      (security 工单创建时低风险直接执行、高风险审批通过后也走此分发,
       修复此前 security 工单审批通过后停在 PENDING 不执行的问题)
    - role:执行角色增删改/权限分配(_execute_role_change)

    幂等:通过 ticket.status=EXECUTED 防重复执行(调用前已置 APPROVED)。
    执行失败抛 ValueError 会回滚审批事务,工单留在 PENDING 可重试。
    """
    if ticket.biz_type == TicketBizType.ORG:
        return _execute_org_change(ticket, actor)
    if ticket.biz_type == TicketBizType.SECURITY:
        return _execute_security_change(ticket)
    if ticket.biz_type == TicketBizType.ROLE:
        return _execute_role_change(ticket, actor)
    if ticket.change_type in (TicketChangeType.GRANT, TicketChangeType.SCOPE_CHANGE):
        _apply_grant(ticket, actor)
    elif ticket.change_type == TicketChangeType.REVOKE:
        _apply_revoke(ticket, actor)
    elif ticket.change_type == TicketChangeType.EXPIRE_EXTEND:
        _apply_extend(ticket, actor)
    elif ticket.change_type == TicketChangeType.ROLE_CHANGE:
        _apply_role_change(ticket, actor)


def _sync_leader_for_role(ticket, role, grant: bool):
    """工单执行时同步组织 leader_id —— 与用户编辑接口 _sync_role_leader 对齐

    业务背景:Team.leader_id / Department.leader_id 与 team_leader / dept_manager 授权
    应保持一致。此前工单授予路径只写授权表不同步 leader_id,导致审批链判定
    部门经理时(基于授权表)与组织树展示(基于 leader_id)不一致。

    规则(与编辑接口对齐):
    - grant: 授予 team_leader/dept_manager 时,仅当原 leader 为空才写入,避免覆盖已有 leader
    - revoke: 撤销时仅当 leader 就是被撤销者才清空,避免误伤
    - scope 过滤:按工单 scope(目标团队/部门)精确定位,ROLE_CHANGE 撤销旧角色时传 previous_role
    """
    if not role:
        return
    role_key = role.role_key
    target = ticket.target_user
    if role_key == 'team_leader':
        team_qs = Team.objects.filter(is_deleted=False)
        if ticket.scope_type == ScopeType.TEAM and ticket.scope_id:
            team_qs = team_qs.filter(id=ticket.scope_id)
        if grant:
            team_qs.filter(leader__isnull=True).update(leader=target)
        else:
            team_qs.filter(leader=target).update(leader=None)
    elif role_key == 'dept_manager':
        dept_qs = Department.objects.filter(is_deleted=False)
        if ticket.scope_type == ScopeType.DEPT and ticket.scope_id:
            dept_qs = dept_qs.filter(id=ticket.scope_id)
        if grant:
            dept_qs.filter(leader__isnull=True).update(leader=target)
        else:
            dept_qs.filter(leader=target).update(leader=None)


def _apply_role_change(ticket: TicketList, actor: User):
    """角色变更执行 —— 原子操作:撤销旧角色(previous_role) + 授予新角色(role)

    业务背景:用户在同一 scope 内变更角色(如 viewer → contributor),
    不能"先撤销后申请"两步走(中间状态会失去权限),必须原子完成。

    流程(全部在同一事务内,任一步失败回滚):
    1. 撤销 previous_role 在 ticket.scope_type/scope_id 下的授权记录
    2. 授予 role(新角色)在 ticket.scope_type/scope_id 下的授权记录
    3. 写 ROLE_CHANGE 审计(包含 previous_role → role 快照)

    边界:
    - previous_role 为空时仅授予新角色(降级到 GRANT 语义)
    - 撤销时只命中 ACTIVE 状态的旧授权,PENDING/REVOKED 不动
    """
    now = timezone.now()

    # 1) 撤销旧角色(若存在)
    if ticket.previous_role_id:
        prev_role = ticket.previous_role
        # 全局角色表（update 返回受影响行数，此处无需判断返回值，直接执行撤销）
        UserRoleRel.objects.filter(
            user=ticket.target_user, role=prev_role, status=GrantStatus.ACTIVE,
        ).update(status=GrantStatus.REVOKED, revoked_at=now, revoked_by=actor, ticket=ticket)
        # 部门属地
        if ticket.scope_type in (ScopeType.DEPT, ScopeType.NONE):
            qs = UserDeptScopeRel.objects.filter(
                user=ticket.target_user, role=prev_role, status=GrantStatus.ACTIVE,
            )
            if ticket.scope_type == ScopeType.DEPT and ticket.scope_id:
                qs = qs.filter(dept_id=ticket.scope_id)
            qs.update(status=GrantStatus.REVOKED, revoked_at=now, revoked_by=actor, ticket=ticket)
        # 团队属地
        if ticket.scope_type in (ScopeType.TEAM, ScopeType.NONE):
            qs = UserTeamScopeRel.objects.filter(
                user=ticket.target_user, role=prev_role, status=GrantStatus.ACTIVE,
            )
            if ticket.scope_type == ScopeType.TEAM and ticket.scope_id:
                qs = qs.filter(team_id=ticket.scope_id)
            qs.update(status=GrantStatus.REVOKED, revoked_at=now, revoked_by=actor, ticket=ticket)
        # 撤销旧管理角色时同步清理组织 leader_id(与 _sync_role_leader 对齐)
        _sync_leader_for_role(ticket, prev_role, grant=False)

    # 2) 授予新角色(复用 _apply_grant 逻辑)
    _apply_grant(ticket, actor)

    # 3) 写 ROLE_CHANGE 审计(独立于 _apply_grant 的 ROLE_GRANT 审计,便于回溯)
    _write_audit(ticket, actor, AuditAction.ROLE_CHANGE, '', '', result='SUCCESS',
                 extra={
                     'previous_role': ticket.previous_role.role_key if ticket.previous_role else None,
                     'new_role': ticket.role.role_key if ticket.role else None,
                 })
    logger.info(f'[Ticket] 角色变更执行: {ticket.ticket_no} '
                f'{ticket.previous_role.role_key if ticket.previous_role else "-"} → '
                f'{ticket.role.role_key if ticket.role else "-"} '
                f'for user {ticket.target_user_id}')


def _apply_grant(ticket: TicketList, actor: User):
    """写入授权表（GRANT/SCOPE_CHANGE）—— 根据 scope_type 分发到三张授权表

    - scope_type=NONE + 全局角色 → UserRoleRel
    - scope_type=DEPT → UserDeptScopeRel
    - scope_type=TEAM → UserTeamScopeRel

    查找条件带 status=ACTIVE:
    - 团队/部门级改为 (user, team/dept) ACTIVE 唯一约束后,可能存在多条历史 REVOKED 记录,
      update_or_create 不带 status 会触发 MultipleObjectsReturned,故只查 ACTIVE 记录。
    - 找到 ACTIVE → update(复用已有记录);找不到 → create(新记录)。
    - 若同 scope 已有不同 role 的 ACTIVE 记录(应用层互斥校验漏了),
      create 会触发 DB 唯一约束报错(EAFP 兜底)。
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
            status=GrantStatus.ACTIVE,
            defaults=common,
        )
        action = AuditAction.SCOPE_GRANT
    elif ticket.scope_type == ScopeType.TEAM and ticket.scope_id:
        UserTeamScopeRel.objects.update_or_create(
            user=ticket.target_user, role=ticket.role, team_id=ticket.scope_id,
            status=GrantStatus.ACTIVE,
            defaults=common,
        )
        action = AuditAction.SCOPE_GRANT
    else:
        # 全局角色（scope_type=NONE）— UserRoleRel 为 (user, role) 绝对唯一,无需带 status
        UserRoleRel.objects.update_or_create(
            user=ticket.target_user, role=ticket.role,
            defaults=common,
        )
        action = AuditAction.ROLE_GRANT
    # 授予 team_leader/dept_manager 时同步组织 leader_id(仅原 leader 为空才写入)
    _sync_leader_for_role(ticket, ticket.role, grant=True)
    _write_audit(ticket, actor, action, '', '', result='SUCCESS')


def _apply_revoke(ticket: TicketList, actor: User):
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
    # 撤销 team_leader/dept_manager 时同步清理组织 leader_id(仅 leader 是被撤销者才清空)
    _sync_leader_for_role(ticket, ticket.role, grant=False)
    _write_audit(ticket, actor, action, '', '',
                 result='SUCCESS' if revoked else 'NOOP')


def _apply_extend(ticket: TicketList, actor: User):
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
