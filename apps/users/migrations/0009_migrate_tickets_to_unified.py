"""数据迁移（第一阶段）：存量工单迁入统一主表 unified_ticket

迁移内容：
1. 清空三张授权表（user_role_global_rel / dept_scope / team_scope）的 ticket_id，
   为 0010 的 FK 改指 unified_ticket 做准备（旧引用在空表中无法通过 FK 校验）
2. permission_approval_ticket（旧权限工单表）→ unified_ticket + permission_ticket_detail
   - 主表承接公共字段（工单号/状态/审批链/时间），权限业务字段入详情子表
   - 授权表旧关联（pat_id -> {表key: [rel pk]}}）暂存到模块级 _LEGACY_REL，
     并冗余写入 unified.detail['legacy_rel']，供 0010 回填（防模块重载丢状态）
3. system_ticket（旧配置/模型/定时任务工单表）→ unified_ticket
   - ticket_type → biz_type；状态枚举映射（pending/pending_review/approved → 新枚举）
   - detail JSON 原样保留；auditor/reviewer 重构为统一审批链节点快照

兼容性说明：
- 旧工单号直接沿用（唯一性由旧表保证），新工单走新格式（类型前缀+日期+当日序列）
- 授权表 ticket_id 回填在 0010（AlterField 之后）执行，通过 detail['legacy_rel'] 关联
- 旧表保留不删除，避免回滚风险；后续确认无引用后再清理
- RunPython 整体事务执行，任一步失败全部回滚
"""
from django.db import migrations

# system_ticket 状态 → 统一主表状态枚举映射
# 注意：system 0013 已把旧 first_approved 归一为 pending_review，这里按现值映射
_SYSTEM_STATUS_MAP = {
    'pending': 'PENDING',
    'pending_review': 'PENDING',   # 待复核 = 仍处于审批流程中
    'approved': 'EXECUTED',        # 旧表 approved = 已通过并生效
    'rejected': 'REJECTED',
    'withdrawn': 'CANCELLED',
}

# system_ticket ticket_type → 统一主表 biz_type 映射
_SYSTEM_TYPE_MAP = {
    'config': 'config',
    'schedule': 'schedule',
    'model': 'model',
}

# system_ticket ticket_type 显示名（历史模型无 get_xxx_display 保障，用映射替代）
_SYSTEM_TYPE_LABEL = {
    'config': '配置变更',
    'schedule': '定时任务',
    'model': '模型变更',
}

# 授权表旧关联暂存（模块级，供 0009→0010 跨迁移函数传递）：
# pat_id -> {rel_model_key: [rel_pk, ...]}，AlterField 后用其回填新工单关联
_LEGACY_REL = {}


def _collect_and_clear_auth_rel_tickets(apps, schema_editor):
    """收集授权表旧工单关联并清空 ticket_id

    授权表 FK 即将从 PermissionApprovalTicket 改指 UnifiedTicket，旧 ticket_id
    在新表中无对应行，必须先行置 NULL；旧关联按 pat 工单暂存到模块级 _LEGACY_REL，
    供 0010 迁移回填（同时写入 unified.detail['legacy_rel'] 落库防丢）。
    """
    global _LEGACY_REL
    rel_specs = (
        ('global', apps.get_model('users', 'UserRoleRel')),
        ('dept', apps.get_model('users', 'UserDeptScopeRel')),
        ('team', apps.get_model('users', 'UserTeamScopeRel')),
    )
    legacy = {}
    for key, model in rel_specs:
        # 先遍历收集旧关联，再统一置空，避免遍历时读到已被 update 置空的记录
        for r in model.objects.exclude(ticket_id__isnull=True).only('id', 'ticket_id'):
            legacy.setdefault(r.ticket_id, {}).setdefault(key, []).append(r.id)
        model.objects.exclude(ticket_id__isnull=True).update(ticket_id=None)
    _LEGACY_REL = legacy


def _migrate_permission_tickets(apps, schema_editor):
    """迁移旧权限工单到统一主表 + 权限详情子表"""
    global _LEGACY_REL
    PermissionApprovalTicket = apps.get_model('users', 'PermissionApprovalTicket')
    UnifiedTicket = apps.get_model('users', 'UnifiedTicket')
    PermissionTicketDetail = apps.get_model('users', 'PermissionTicketDetail')

    for old in PermissionApprovalTicket.objects.all().order_by('id'):
        # 任务名：便于工单中心列表展示与模糊搜索
        role_key = old.role.role_key if old.role_id else ''
        title = f'权限·{old.change_type} {role_key}'.strip()
        # 授权表旧关联随工单落库（临时字段，0010 回填后无业务意义）
        legacy_rel = _LEGACY_REL.get(old.id) or {}
        new = UnifiedTicket.objects.create(
            ticket_no=old.ticket_no,
            title=title,
            biz_type='permission',
            status=old.status,
            risk_level='normal',
            applicant_id=old.applicant_id,
            approval_chain=old.approval_chain or [],
            current_step=old.current_step or 0,
            detail={'legacy_rel': legacy_rel} if legacy_rel else {},
            approved_at=old.approved_at,
            executed_at=old.executed_at,
        )
        # 保留旧创建时间（auto_now_add 会覆盖，需手动回填）
        if old.created_at:
            UnifiedTicket.objects.filter(pk=new.pk).update(created_at=old.created_at)
        PermissionTicketDetail.objects.create(
            ticket_id=new.pk,
            target_user_id=old.target_user_id,
            change_type=old.change_type,
            role_id=old.role_id,
            previous_role_id=old.previous_role_id,
            scope_type=old.scope_type,
            scope_id=old.scope_id,
            effective_from=old.effective_from,
            expires_at=old.expires_at,
            reason=old.reason or '',
        )


def _build_system_chain(old, apps):
    """为旧 config/model/schedule 工单重构统一审批链节点快照

    旧审批为固定两级（auditor 审核 + reviewer 超管复核），映射为：
    - pending：审核节点 PENDING
    - pending_review：审核节点已通过 + 复核节点 PENDING
    - approved（已生效）：审核节点已通过（若有 reviewer 再补复核节点）
    - rejected：将首个未通过节点标记为 REJECTED（无法还原具体驳回节点）
    - withdrawn：仅还原已处理节点快照
    节点 approver_role：审核=SYSTEM_AUDITOR，复核=SUPER_ADMIN
    """
    nodes = []
    if old.auditor_id:
        nodes.append({
            'approver_role': 'SYSTEM_AUDITOR',
            'approver_scope_type': 'NONE',
            'approver_scope_id': None,
            'approver_id': old.auditor_id,
            'status': 'APPROVED' if old.audited_at else 'PENDING',
            'approved_at': old.audited_at.isoformat() if old.audited_at else None,
            'comment': old.audit_comment or '',
        })
    if old.reviewer_id:
        nodes.append({
            'approver_role': 'SUPER_ADMIN',
            'approver_scope_type': 'NONE',
            'approver_scope_id': None,
            'approver_id': old.reviewer_id,
            'status': 'APPROVED' if old.reviewed_at else 'PENDING',
            'approved_at': old.reviewed_at.isoformat() if old.reviewed_at else None,
            'comment': old.review_comment or '',
        })
    # pending 且无 auditor（早期工单可能缺 auditor），补一个待审批节点
    if not nodes and old.status == 'pending':
        nodes.append({
            'approver_role': 'SYSTEM_AUDITOR',
            'approver_scope_type': 'NONE',
            'approver_scope_id': None,
            'approver_id': None,
            'status': 'PENDING',
            'approved_at': None,
            'comment': '',
        })
    # 被驳回的工单：第一个未通过节点标记为 REJECTED，保持审批链可读
    if old.status == 'rejected':
        for n in nodes:
            if n['status'] != 'APPROVED':
                n['status'] = 'REJECTED'
                break
        else:
            if nodes:
                nodes[-1]['status'] = 'REJECTED'
    return nodes


def _migrate_system_tickets(apps, schema_editor):
    """迁移旧 system_ticket（config/model/schedule）到统一主表"""
    SystemTicket = apps.get_model('system', 'Ticket')
    UnifiedTicket = apps.get_model('users', 'UnifiedTicket')

    for old in SystemTicket.objects.all().order_by('id'):
        status = _SYSTEM_STATUS_MAP.get(old.status, 'PENDING')
        biz_type = _SYSTEM_TYPE_MAP.get(old.ticket_type, 'config')
        detail = dict(old.detail) if isinstance(old.detail, dict) else {}
        # 变更原因冗余进 detail，工单中心详情可展示
        if old.reason:
            detail.setdefault('reason', old.reason)
        # 任务名：配置/定时任务用配置项中文名，模型用操作类型
        type_label = _SYSTEM_TYPE_LABEL.get(old.ticket_type, old.ticket_type or '')
        if biz_type in ('config', 'schedule'):
            label = detail.get('config_label') or old.config_key or ''
            title = f'{type_label} {label}'.strip()
        else:
            title = f'模型变更 {old.operation or ""}'.strip()
        chain = _build_system_chain(old, apps)
        # 当前节点索引 = 已通过节点数（统一流程引擎约定）
        current_step = 0
        for n in chain:
            if n['status'] != 'APPROVED':
                break
            current_step += 1
        new = UnifiedTicket.objects.create(
            ticket_no=f'ST{old.id:08d}',   # 旧表无工单号，用固定前缀+旧 id 保证唯一
            title=title,
            biz_type=biz_type,
            status=status,
            risk_level=old.risk_level or 'normal',
            applicant_id=old.creator_id,
            approval_chain=chain,
            current_step=current_step,
            detail=detail,
            operation=old.operation or '',
            config_key=old.config_key,
            target_model_id=old.target_model_id,
            approved_at=old.applied_at,
            executed_at=old.applied_at,
        )
        if old.created_at:
            UnifiedTicket.objects.filter(pk=new.pk).update(created_at=old.created_at)


def migrate_tickets_stage1(apps, schema_editor):
    """第一阶段：清空授权引用 → 迁移工单数据"""
    _collect_and_clear_auth_rel_tickets(apps, schema_editor)
    _migrate_permission_tickets(apps, schema_editor)
    _migrate_system_tickets(apps, schema_editor)


def reverse_tickets(apps, schema_editor):
    """回滚：删除统一主表数据（旧表数据已保留，不反向恢复）"""
    UnifiedTicket = apps.get_model('users', 'UnifiedTicket')
    PermissionTicketDetail = apps.get_model('users', 'PermissionTicketDetail')
    TicketFlowLog = apps.get_model('users', 'TicketFlowLog')
    TicketFlowLog.objects.all().delete()
    PermissionTicketDetail.objects.all().delete()
    UnifiedTicket.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0008_unifiedticket_ticketflowlog_permissionticketdetail_and_more'),
        ('system', '0013_migrate_data_to_ticket'),
    ]

    operations = [
        migrations.RunPython(migrate_tickets_stage1, reverse_tickets),
    ]
