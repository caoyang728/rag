"""工单视图：权限审批(通过/驳回) + 统一工单中心（全部类型工单一页展示）

- TicketApproveView / TicketRejectView：权限域共享审批池审批（走 ticket_service）
- TicketCenter*：统一入口，按工单类型路由（permission/org/security 走共享审批池，
  config/schedule/model 委托 system 侧视图）
- 审批链序列化（_serialize_center_ticket）为输出侧逻辑，保留在本模块；
  审批池权限判定（_can_approve_for_role）复用 ticket_service。
"""
from loguru import logger
from django.db import models

from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.models import (
    Department, Team,
    TicketList, TicketStatus, TicketBizType, TicketChangeType, ScopeType,
    SecurityConfigType, SecurityOperation, OrgChangeType, OrgOperation,
    UserRoleRel, GrantStatus, has_permission,
)
from apps.users.ticket_service import cancel_ticket, _can_approve_for_role, parse_change_summary
from apps.users.services.ticket_base import get_approved_approver_ids
from apps.users.utils import _client_ip, _serialize_chain_nodes, _client_ua, _resolve_scope_name


class TicketApproveView(APIView):
    """POST /api/v1/auth/permissions/tickets/<id>/approve/
    审批通过工单（共享审批池模式：任一匹配 approver_role 的用户均可审批）

    Body:
    - comment: string，审批意见（可选）
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            ticket = TicketList.objects.get(pk=pk)
        except TicketList.DoesNotExist:
            return Response({"detail": "工单不存在"}, status=404)

        if ticket.status != TicketStatus.PENDING:
            return Response({"detail": f"工单非待审批状态: {ticket.status}"}, status=400)

        chain = ticket.approval_chain or []
        if ticket.current_step >= len(chain):
            return Response({"detail": "审批链已完结，无待审批节点"}, status=400)

        node = chain[ticket.current_step]
        approver_role = node['approver_role']

        # 角色匹配校验 + 重复处理校验
        if node.get('approver_id') and node['approver_id'] != request.user.id:
            raise PermissionDenied("该工单已被其他管理员处理，不再属于您的待办")
        if not _can_approve_for_role(request.user, approver_role, ticket):
            raise PermissionDenied(f"您没有审批 {approver_role} 类型工单的权限")

        comment = (request.data.get("comment") or "").strip()
        ip = _client_ip(request)
        ua = _client_ua(request)

        # 延迟导入：保持测试可 patch apps.users.ticket_service.approve_ticket
        from apps.users.ticket_service import approve_ticket
        try:
            ticket = approve_ticket(ticket, request.user, comment=comment, ip_address=ip, user_agent=ua)
        except (ValueError, PermissionError) as e:
            return Response({"detail": str(e)}, status=400)

        logger.info(f"Ticket approved: id={pk}, ticket_no={ticket.ticket_no}, approver={request.user.username}")
        return Response({
            "ok": True,
            "ticket_no": ticket.ticket_no,
            "status": ticket.status,
            "current_step": ticket.current_step,
            "total_steps": len(ticket.approval_chain or []),
        })


class TicketRejectView(APIView):
    """POST /api/v1/auth/permissions/tickets/<id>/reject/
    驳回工单（拒绝必填理由）

    Body:
    - comment: string，驳回理由（必填）
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        comment = (request.data.get("comment") or "").strip()
        if not comment:
            return Response({"detail": "驳回理由不能为空"}, status=400)

        try:
            ticket = TicketList.objects.get(pk=pk)
        except TicketList.DoesNotExist:
            return Response({"detail": "工单不存在"}, status=404)

        if ticket.status != TicketStatus.PENDING:
            return Response({"detail": f"工单非待审批状态: {ticket.status}"}, status=400)

        chain = ticket.approval_chain or []
        # 当前节点审批人 或 super_admin 可驳回
        can_reject = False
        if ticket.current_step < len(chain):
            node = chain[ticket.current_step]
            if node.get('approver_id') and node['approver_id'] == request.user.id:
                can_reject = True
            elif _can_approve_for_role(request.user, node['approver_role'], ticket):
                can_reject = True
        if not can_reject and not request.user.is_super_admin:
            raise PermissionDenied("无权驳回该工单")

        ip = _client_ip(request)
        ua = _client_ua(request)

        # 延迟导入：保持测试可 patch apps.users.ticket_service.reject_ticket
        from apps.users.ticket_service import reject_ticket
        try:
            ticket = reject_ticket(ticket, request.user, comment=comment, ip_address=ip, user_agent=ua)
        except (ValueError, PermissionError) as e:
            return Response({"detail": str(e)}, status=400)

        logger.info(f"Ticket rejected: id={pk}, ticket_no={ticket.ticket_no}, rejector={request.user.username}")
        return Response({
            "ok": True,
            "ticket_no": ticket.ticket_no,
            "status": ticket.status,
            "reject_comment": comment,
        })


# ============================================================================
# 统一工单中心（方案1：TicketList 主表，全部类型工单一页展示）
# 覆盖 权限审批/配置变更/定时任务/模型变更 四类工单，用筛选器区分。
# 列表 API：GET /api/v1/auth/tickets/
# 操作 API：POST /api/v1/auth/tickets/<pk>/approve|reject|withdraw/
# ============================================================================

# 操作类型显示映射（对齐 system_ticket.OPERATION_CHOICES，避免跨 app 依赖）
_CENTER_OPERATION_DISPLAY = {
    'modify': '修改配置',
    'update_normal': '修改模型',
    'deactivate': '停用模型',
    'delete': '删除模型',
}


def _ticket_visible_scope(user):
    """工单中心展示矩阵：按角色计算当前用户可见工单的范围条件（Q）

    所有登录用户均可访问工单中心，可见范围按角色叠加（并集）：
    - super_admin / compliance_admin：全部工单（返回 None 表示不限）
    - user_admin（user.manage_all / user.manage）：所有角色操作相关工单
      （permission 域且详情子表 role 非空，即用户角色授权/变更）
    - maintain_admin（system.config.write）：所有配置相关工单（config/schedule/model）
    - kb_admin（kb.manage_all）：所有知识库相关工单
      （permission 域且详情子表 role 为空，即文档/节点授权）
    - 部门经理（Department.leader_id）：申请人或目标用户属于管辖部门
    - 组长（Team.leader_id）：申请人或目标用户属于管辖团队
    - 兜底：自己的工单始终可见（个人视角）
    """
    if getattr(user, 'is_super_admin', False) or getattr(user, 'is_compliance_admin', False):
        return None

    q = models.Q(applicant=user)

    # user_admin：用户角色授权/变更类权限工单（详情子表 role 非空）
    # 组织变更工单（部门/团队增删改）也归用户管理员管辖，与角色工单同视角
    if has_permission(user, 'user.manage_all') or has_permission(user, 'user.manage'):
        q |= models.Q(biz_type=TicketBizType.PERMISSION, permission_detail__role__isnull=False)
        q |= models.Q(biz_type=TicketBizType.ORG)

    # maintain_admin：配置/定时任务/模型变更工单
    if has_permission(user, 'system.config.write'):
        q |= models.Q(biz_type__in=(TicketBizType.CONFIG, TicketBizType.SCHEDULE, TicketBizType.MODEL))

    # kb_admin：文档/节点授权类权限工单（详情子表 role 为空，见 _create_doc_ticket）
    if has_permission(user, 'kb.manage_all'):
        q |= models.Q(biz_type=TicketBizType.PERMISSION, permission_detail__role__isnull=True)

    # 部门经理：申请人或目标用户属于管辖部门（一人可管辖多部门）
    dept_ids = list(Department.objects.filter(leader_id=user.id, is_deleted=False).values_list('id', flat=True))
    if dept_ids:
        q |= models.Q(applicant__department_id__in=dept_ids) | \
            models.Q(permission_detail__target_user__department_id__in=dept_ids)

    # 组长：申请人或目标用户属于管辖团队（一人可管辖多团队，支持跨部门）
    team_ids = list(Team.objects.filter(leader_id=user.id, is_deleted=False).values_list('id', flat=True))
    if team_ids:
        q |= models.Q(applicant__team_id__in=team_ids) | \
            models.Q(permission_detail__target_user__team_id__in=team_ids)

    return q


# 统一工单审批链最大节点数（权限域最多 3 节点，系统域最多 2 节点），
# 用于 pending/processed 视角对 approval_chain JSON 数组做 DB 侧粗过滤
_MAX_CHAIN_STEPS = 4


def _user_approvable_roles(user):
    """计算用户可审批的审批链节点角色集合 —— 待我审批视角的 DB 侧粗过滤

    只做"用户拥有哪些角色身份"的粗筛（super_admin/SYSTEM_AUDITOR/管理角色/leader），
    TEAM_LEADER/DEPT_LEADER 的 scope 是否匹配、回避原则与双人独立性
    留给 _can_user_approve_ticket 逐条精判，避免把不可审批工单捞进候选集后逐条查库。
    """
    roles = set()
    if getattr(user, 'is_super_admin', False):
        roles.add('SUPER_ADMIN')
    if user.has_perm('system.config.write'):
        roles.add('SYSTEM_AUDITOR')
    # 管理角色：用户管理员/知识管理员/部门经理（角色授权在 UserRoleRel）
    for rk in UserRoleRel.objects.filter(
        user=user, status=GrantStatus.ACTIVE,
        role__role_key__in=('user_admin', 'kb_admin', 'dept_manager'),
    ).values_list('role__role_key', flat=True).distinct():
        roles.add({'user_admin': 'USER_ADMIN', 'kb_admin': 'KB_ADMIN',
                   'dept_manager': 'DEPT_LEADER'}[rk])
    # leader 身份绑定在 Team/Department.leader_id 上，scope 是否匹配留精判
    if Team.objects.filter(leader_id=user.id, is_deleted=False).exists():
        roles.add('TEAM_LEADER')
    if Department.objects.filter(leader_id=user.id, is_deleted=False).exists():
        roles.add('DEPT_LEADER')
    return roles


def _can_user_approve_ticket(user, ticket):
    """判定用户是否可审批统一工单的当前节点（跨权限域）

    支持两类审批链节点：
    - 系统域角色 SYSTEM_AUDITOR：配置/模型/定时工单审核节点，需 system.config.write 权限，
      且创建人不能审自己提交的工单（防自审）
    - SUPER_ADMIN 与权限域角色（TEAM_LEADER/DEPT_LEADER/USER_ADMIN/KB_ADMIN）：
      统一走 ticket_service._can_approve_for_role 共享审批池判定
      （含回避原则 + 双人独立性，super_admin 复核天然防同一人独审两节点）
    文档/节点访问工单（approver_role 为空）：审批人绑定资源所有者，
    由知识域 approve_access_request 独立处理，工单中心不将其列为待我审批。
    """
    if not getattr(user, 'is_authenticated', False) or ticket.status != TicketStatus.PENDING:
        return False
    chain = ticket.approval_chain or []
    if ticket.current_step >= len(chain):
        return False
    role = (chain[ticket.current_step] or {}).get('approver_role')
    if not role:
        return False
    if role == 'SYSTEM_AUDITOR':
        if not user.has_perm('system.config.write'):
            return False
        # 防自审：创建人不能审自己提交的工单（避免自己发起的工单出现在待我审批）
        if ticket.applicant_id == user.id:
            return False
        # 双人独立性：已审过该工单任一前序节点的人不能再审后续节点，
        # 与权限域 _can_approve_for_role 共用公共判定，避免逻辑漂移
        if user.id in get_approved_approver_ids(ticket):
            return False
        return True
    return _can_approve_for_role(user, role, ticket)


def _serialize_center_ticket(t, config_map=None, model_map=None, dept_map=None, team_map=None):
    """序列化统一工单中心行数据 —— 公共字段 + 类型特有业务字段

    公共字段：工单号/任务名/类型/状态/风险/发起人/审批进度/时间
    类型特有（前端按 biz_type 渲染不同卡片）：
    - permission：变更类型/目标用户/角色/范围/有效期/理由（TicketPermissionDetail 子表）
    - config/schedule：配置项/旧新值/变更摘要（TicketConfigDetail/TicketScheduleDetail 子表）
    - model：模型名/操作类型/变更字段（TicketModelDetail 子表）
    非本类型的字段统一补空值，保证前端 schema 稳定。
    config_map/model_map/dept_map/team_map 为列表接口批量预加载的缓存，避免 N+1 查询。
    业务字段统一经主表代理属性读取，不再解包主表 JSON。
    """
    config_map = config_map or {}
    model_map = model_map or {}
    dept_map = dept_map or {}
    team_map = team_map or {}
    chain = t.approval_chain or []

    row = {
        'id': t.id,
        'ticket_no': t.ticket_no,
        'title': t.title,
        'biz_type': t.biz_type,
        'biz_type_display': t.get_biz_type_display(),
        'status': t.status,
        'status_display': dict(TicketStatus.choices).get(t.status, t.status),
        'risk_level': t.risk_level,
        'applicant_id': t.applicant_id,
        'applicant_name': t.applicant.real_name or t.applicant.username if t.applicant else '',
        'applicant_username': t.applicant.username if t.applicant else '',
        'applicant_email': t.applicant.email if t.applicant else '',
        'created_at': t.created_at.isoformat() if t.created_at else '',
        'updated_at': t.updated_at.isoformat() if t.updated_at else '',
        'approved_at': t.approved_at.isoformat() if t.approved_at else '',
        'executed_at': t.executed_at.isoformat() if t.executed_at else '',
        'current_step': t.current_step,
        'total_steps': len(chain),
        'approval_chain': _serialize_chain_nodes(chain),
        # 类型特有字段占位（下方按 biz_type 填充）
        'change_type': None,
        'change_type_display': '',
        'target_user_id': None,
        'target_user_name': '',
        'target_user_username': '',
        'target_user_email': '',
        'role_id': None,
        'role_name': '',
        'role_key': '',
        'previous_role_name': '',
        'scope_type': '',
        'scope_id': None,
        'scope_name': '',
        'reason': '',
        'effective_from': '',
        'expires_at': '',
        'config_key': None,
        'config_label': '',
        'old_value': '',
        'new_value': '',
        'change_summary': None,
        'model_name': '',
        'target_model_name': '',
        'operation': t.operation,
        'operation_display': _CENTER_OPERATION_DISPLAY.get(t.operation, t.operation),
        'changed_fields': [],
        # security 特有字段占位
        'security_type': '',
        'security_type_display': '',
        'security_target': '',
        # org 特有字段占位
        'org_type': '',
        'org_type_display': '',
        'org_name': '',
        'old_data': None,
        'new_data': None,
    }

    # --- permission：详情子表（change_type/role/scope 等） ---
    if t.biz_type == TicketBizType.PERMISSION:
        # scope_name 走列表接口批量预加载的 dept_map/team_map，避免逐条查询
        scope_name = _resolve_scope_name(t.scope_type, t.scope_id, dept_map, team_map)
        row.update({
            'change_type': t.change_type,
            'change_type_display': dict(TicketChangeType.choices).get(t.change_type, t.change_type),
            'target_user_id': t.target_user_id,
            'target_user_name': t.target_user.real_name or t.target_user.username if t.target_user else '',
            'target_user_username': t.target_user.username if t.target_user else '',
            'target_user_email': t.target_user.email if t.target_user else '',
            'role_id': t.role_id,
            'role_name': t.role.name if t.role else '',
            'role_key': t.role.role_key if t.role else '',
            'previous_role_name': t.previous_role.name if t.previous_role else '',
            'scope_type': t.scope_type,
            'scope_id': t.scope_id,
            'scope_name': scope_name,
            'reason': t.reason or '',
            'effective_from': t.effective_from.isoformat() if t.effective_from else '',
            'expires_at': t.expires_at.isoformat() if t.expires_at else '',
        })
    else:
        # config/schedule/model：reason 经主表代理属性读取对应详情子表
        row['reason'] = t.reason or ''
        # --- config/schedule：配置项/旧新值/变更摘要（TicketConfigDetail/TicketScheduleDetail） ---
        if t.biz_type in (TicketBizType.CONFIG, TicketBizType.SCHEDULE):
            cfg = config_map.get(t.config_key) if t.config_key else None
            is_secret = bool(cfg and cfg.is_secret)
            old_value = '***' if is_secret else t.old_value
            new_value = '***' if is_secret else t.new_value
            # change_summary 为 JSON 字符串，解析失败返回 None（前端按无差异摘要展示）
            change_summary = parse_change_summary(t.change_summary)
            row.update({
                'config_key': t.config_key,
                'config_label': t.config_label,
                'old_value': old_value,
                'new_value': new_value,
                'change_summary': change_summary,
            })
        # --- model：模型名/变更字段（TicketModelDetail） ---
        elif t.biz_type == TicketBizType.MODEL:
            target_model_name = ''
            if t.target_model_id:
                m = model_map.get(t.target_model_id)
                if m:
                    target_model_name = f'{m.name} ({m.model_name})'
            row.update({
                'model_name': target_model_name,
                'target_model_name': target_model_name,
                'changed_fields': list((t.changed_fields or {}).keys()),
            })
        # --- agent：Agent 工作流人工确认（TicketAgentApprovalDetail） ---
        # reason 已带 [agent:{wf_id}:approval] 前缀，工单中心可直接展示确认理由
        elif t.biz_type == TicketBizType.AGENT:
            ad = getattr(t, 'agent_approval_detail', None)
            row['reason'] = ad.reason if ad else ''
            row['operation'] = t.operation or 'agent_approval'
        # --- security：安全配置（TicketSecurityDetail），目标取 target_data 快照 ---
        elif t.biz_type == TicketBizType.SECURITY:
            sd = getattr(t, 'security_detail', None)
            if sd:
                target_data = sd.target_data or {}
                # 目标展示值:白名单/黑名单取 ip_pattern,敏感词取 word
                target_val = target_data.get('ip_pattern') or target_data.get('word') or ''
                row.update({
                    'security_type': sd.security_type,
                    'security_type_display': dict(SecurityConfigType.choices).get(sd.security_type, sd.security_type),
                    'operation': sd.operation,
                    'operation_display': dict(SecurityOperation.choices).get(sd.operation, sd.operation),
                    'security_target': target_val,
                    'reason': sd.reason or '',
                })
        # --- org：组织变更（TicketOrgDetail），old/new 直接读子表 JSON ---
        elif t.biz_type == TicketBizType.ORG:
            od = getattr(t, 'org_detail', None)
            row['reason'] = od.reason if od and od.reason else (row.get('reason') or '')
            if od:
                # operation_display 由 组织类型+操作 组合计算（主表 _CENTER_OPERATION_DISPLAY 仅覆盖模型域）
                org_op_labels = {
                    (OrgChangeType.DEPT, OrgOperation.ADD): '部门新增',
                    (OrgChangeType.DEPT, OrgOperation.EDIT): '部门编辑',
                    (OrgChangeType.DEPT, OrgOperation.DELETE): '部门删除',
                    (OrgChangeType.TEAM, OrgOperation.ADD): '团队新增',
                    (OrgChangeType.TEAM, OrgOperation.EDIT): '团队编辑',
                    (OrgChangeType.TEAM, OrgOperation.DELETE): '团队删除',
                }
                row.update({
                    'org_type': od.org_type,
                    'org_type_display': dict(OrgChangeType.choices).get(od.org_type, od.org_type),
                    'operation': od.operation,
                    'operation_display': org_op_labels.get((od.org_type, od.operation),
                                                           od.operation),
                    # target_data 为创建时的目标快照(名称等),部门/团队可能已被其他工单变更,展示用快照
                    'org_name': (od.target_data or {}).get('name', ''),
                    'old_data': od.old_data,
                    'new_data': od.new_data,
                    'changed_fields': sorted(set((od.old_data or {}).keys()) | set((od.new_data or {}).keys())),
                })
    return row


class TicketCenterView(APIView):
    """GET /api/v1/users/tickets/  统一工单中心列表（全部类型工单一页展示）

    对所有登录用户开放，可见范围按角色过滤（_ticket_visible_scope）：
    super_admin 全量；user_admin 角色操作工单；maintain_admin 配置类工单；
    kb_admin 知识库工单；部门经理/组长按管辖部门/团队归属；个人仅自己的工单。

    视图维度 view（默认 pending）：
    - pending   待我审批：共享审批池中当前用户可审批的 PENDING 工单（权限域 + 系统域）
    - processed 我已审批：当前用户在审批链上任一步骤处理过的工单
    - mine      我的工单：当前用户发起的工单（所有状态）
    - all       全部工单：按角色可见范围展示（不限于 super_admin）

    过滤参数：
    - type    工单类型（permission/config/model/schedule，逗号分隔多值，默认全部）
    - status  状态（PENDING/APPROVED/EXECUTED/REJECTED/CANCELLED，逗号分隔多值）
    - search  搜索：工单id/工单号/创建人 username 精确匹配 + 任务名模糊匹配

    分页：page（默认 1）/ page_size（默认 20，最大 100）
    返回：{"rows": [...], "count": 总数}
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        view = (request.query_params.get('view') or 'pending').strip()

        # 工单中心对所有登录用户开放；可见范围按角色过滤（见 _ticket_visible_scope）。
        # 角色范围只约束浏览视图（all/mine）；pending（待我审批）/processed（我已审批）
        # 保持审批池/处理记录语义：以审批链判定为准，不额外收窄，避免漏掉可审批/已审批工单。
        # 可见范围计算涉及部门/团队归属查询，仅浏览视图才延迟计算（审批视图短路）。
        if view not in ('pending', 'processed'):
            visible = _ticket_visible_scope(user)
        else:
            visible = None

        # 类型过滤（逗号分隔多值）
        type_param = (request.query_params.get('type') or '').strip()
        type_list = [x.strip() for x in type_param.split(',') if x.strip()]
        valid_types = {c[0] for c in TicketBizType.choices}
        if any(x not in valid_types for x in type_list):
            return Response(
                {"detail": f"type 取值非法，支持 {'/'.join(sorted(valid_types))}"},
                status=400,
            )

        # 状态过滤（逗号分隔多值）
        status_param = (request.query_params.get('status') or '').strip()
        status_list = [s.strip().upper() for s in status_param.split(',') if s.strip()]
        valid_status = {c[0] for c in TicketStatus.choices}
        if any(s not in valid_status for s in status_list):
            return Response(
                {"detail": f"status 取值非法，支持 {'/'.join(sorted(valid_status))}"},
                status=400,
            )

        # 搜索：id/工单号/创建人 username 精确 + 任务名模糊
        search = (request.query_params.get('search') or '').strip()[:100]

        qs = TicketList.objects.select_related(
            'applicant', 'permission_detail__target_user',
            'permission_detail__role', 'permission_detail__previous_role',
            'config_detail', 'schedule_detail', 'model_detail',
            'org_detail', 'security_detail',
        )
        if type_list:
            qs = qs.filter(biz_type__in=type_list)
        if status_list:
            qs = qs.filter(status__in=status_list)
        if search:
            q = models.Q()
            if search.isdigit():
                q |= models.Q(id=int(search))
            q |= models.Q(ticket_no__iexact=search)
            q |= models.Q(applicant__username__iexact=search)
            q |= models.Q(title__icontains=search)
            qs = qs.filter(q)

        # 可见范围过滤：浏览视图（mine/all）受角色范围约束；审批视图不额外收窄
        if view not in ('pending', 'processed') and visible is not None:
            qs = qs.filter(visible)

        # 视角过滤：mine 直接按发起人；pending/processed 先取候选再 Python 侧过滤
        if view == 'mine':
            qs = qs.filter(applicant=user)
        elif view == 'pending':
            qs = qs.filter(status=TicketStatus.PENDING)
        elif view == 'processed':
            qs = qs.exclude(status=TicketStatus.PENDING)
        qs = qs.order_by('-created_at')

        if view == 'pending':
            # 待我审批：先用用户可审批角色集合对 approval_chain 做 DB 侧粗过滤，
            # 替代原先 qs[:2000] 截断后再 Python 逐条过滤（窗口外的可审批工单会被静默漏掉）。
            # 角色 scope 是否匹配、回避原则与双人独立性仍由 _can_user_approve_ticket 逐条精判。
            approvable_roles = _user_approvable_roles(user)
            if not approvable_roles:
                # 无任何可审批角色身份（普通员工），待办必然为空，直接短路
                candidates = []
            else:
                # 审批链最多 _MAX_CHAIN_STEPS 个节点，当前节点 index 各工单不同，
                # 故对每个可能 index 的 approver_role 做 OR 匹配（index 越界时 jsonb 返回 NULL 不命中）
                role_q = models.Q()
                for i in range(_MAX_CHAIN_STEPS):
                    role_q |= models.Q(**{f'approval_chain__{i}__approver_role__in': approvable_roles})
                qs = qs.filter(role_q)
                candidates = [t for t in qs.iterator() if _can_user_approve_ticket(user, t)]
        elif view == 'processed':
            # 我已审批：DB 侧粗过滤审批链 JSON 中包含当前用户 id 的工单（jsonb 包含语义，
            # 元素可含其他键），避免逐条全表扫描；approver_id 精确相等仍由下方确认。
            qs = qs.filter(approval_chain__contains=[{'approver_id': user.id}])
            candidates = [
                t for t in qs.iterator()
                if any(n.get('approver_id') == user.id for n in (t.approval_chain or []))
            ]
        else:
            candidates = list(qs)
        total = len(candidates)

        # 分页
        try:
            page = max(int(request.query_params.get('page', 1)), 1)
            page_size = max(1, min(int(request.query_params.get('page_size', 20)), 100))
        except (ValueError, TypeError):
            return Response({"detail": "page/page_size 参数无效"}, status=400)
        start = (page - 1) * page_size
        page_rows = candidates[start:start + page_size]

        # 批量预加载 config/model 关联，避免 N+1
        config_keys = {t.config_key for t in page_rows if t.config_key}
        model_ids = {t.target_model_id for t in page_rows if t.target_model_id}
        from apps.system.models import SystemConfig, LLMModel
        config_map = {c.key: c for c in SystemConfig.objects.filter(key__in=config_keys)} if config_keys else {}
        model_map = {m.id: m for m in LLMModel.objects.filter(id__in=model_ids)} if model_ids else {}

        # 批量预加载 permission 工单 scope_name 所需部门/团队（scope_name 渲染不逐条查库）
        dept_ids = {
            t.scope_id for t in page_rows
            if t.biz_type == TicketBizType.PERMISSION and t.scope_type == ScopeType.DEPT and t.scope_id
        }
        team_ids = {
            t.scope_id for t in page_rows
            if t.biz_type == TicketBizType.PERMISSION and t.scope_type == ScopeType.TEAM and t.scope_id
        }
        dept_map = {d.id: d.name for d in Department.objects.filter(
            id__in=dept_ids, is_deleted=False).only('id', 'name')} if dept_ids else {}
        team_map = {tm.id: tm.name for tm in Team.objects.filter(
            id__in=team_ids, is_deleted=False).only('id', 'name')} if team_ids else {}

        rows = [_serialize_center_ticket(t, config_map, model_map, dept_map, team_map) for t in page_rows]
        return Response({'rows': rows, 'count': total})


class TicketCenterApproveView(APIView):
    """POST /api/v1/auth/tickets/<pk>/approve/  统一审批通过入口

    按工单类型路由到对应审批逻辑：
    - permission：共享审批池审批（ticket_service.approve_ticket）
    - config/schedule/model：系统工单审批（审核+超管复核+执行生效，复用 system 视图）
    前端无需感知类型差异，工单中心统一入口即可。
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        ticket = TicketList.objects.filter(pk=pk).first()
        if not ticket:
            return Response({"detail": "工单不存在"}, status=404)
        # 共享审批池类型（权限/组织变更/IP黑白名单）：统一走 ticket_service 审批，
        # 类型无关，回避原则/双人独立性由 _can_approve_for_role 保证，执行由分发函数落地
        if ticket.biz_type in (TicketBizType.PERMISSION, TicketBizType.ORG, TicketBizType.SECURITY):
            return TicketApproveView().post(request, pk)
        # 系统域工单：委托 system 侧审批视图（内部含权限校验/依赖检查/执行生效）
        from apps.system.views import ApproveTicketView
        return ApproveTicketView().post(request, pk)


class TicketCenterRejectView(APIView):
    """POST /api/v1/users/tickets/<pk>/reject/  统一驳回入口

    按工单类型路由：permission 走共享审批池驳回；config/schedule/model 走系统驳回。
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        ticket = TicketList.objects.filter(pk=pk).first()
        if not ticket:
            return Response({"detail": "工单不存在"}, status=404)
        if ticket.biz_type in (TicketBizType.PERMISSION, TicketBizType.ORG, TicketBizType.SECURITY):
            return TicketRejectView().post(request, pk)
        from apps.system.views import RejectTicketView
        return RejectTicketView().post(request, pk)


class TicketCenterWithdrawView(APIView):
    """POST /api/v1/auth/tickets/<pk>/withdraw/  创建人撤回统一入口

    按工单类型路由：permission 走 cancel_ticket；config/schedule/model 走系统撤回。
    仅创建人本人可操作，且仅 PENDING 状态可撤回。
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        ticket = TicketList.objects.filter(pk=pk).first()
        if not ticket:
            return Response({"detail": "工单不存在"}, status=404)
        if ticket.biz_type in (TicketBizType.PERMISSION, TicketBizType.ORG, TicketBizType.SECURITY):
            # 与 AccessApplicationWithdrawView 一致：仅创建人 + PENDING
            if ticket.applicant_id != request.user.id:
                return Response({"detail": "仅创建人可撤回工单"}, status=403)
            if ticket.status != TicketStatus.PENDING:
                return Response({"detail": f"当前状态 {ticket.status} 不可撤回"}, status=400)
            cancel_ticket(
                ticket, request.user,
                ip_address=_client_ip(request),
                user_agent=_client_ua(request),
            )
            return Response({"detail": "已撤回", "status": ticket.status})
        from apps.system.views import WithdrawTicketView
        return WithdrawTicketView().post(request, pk)
