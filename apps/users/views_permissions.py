"""权限视图：我的权限 / 审批人(废弃) / 权限申请 / 可申请角色 / 审批链预览

AccessApplicationView 的 POST 参数校验已序列化器化（AccessApplicationSerializer，
仅协议层：类型/枚举/必填/参数间约束）；角色存在性、管理岗任命权限、资源所有者判定、
previous_role 解析等业务校验仍留在视图，落库统一走 ticket_service.create_ticket。
可申请角色清单与审批链预览分别复用 services/access_service。
"""
import datetime
from loguru import logger
from django.utils import timezone

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.models import (
    Department, Team, Role, Permission, User,
    ScopeType, TicketList, TicketStatus, TicketChangeType,
    get_user_permissions, UserStatus,
)
from apps.users.serializers import AccessApplicationSerializer
from apps.users.services.access_service import (
    can_nominate, is_resource_owner, get_assignable_roles, preview_approval_chain,
    SELF_APPLY_FORBIDDEN_KEYS,
)
from apps.users.ticket_service import cancel_ticket
from apps.users.utils import _client_ip, _serialize_chain_nodes, _first_serializer_error, _client_ua, _resolve_scope_name


class MyPermissionsView(APIView):
    """GET /api/v1/auth/permissions/me/
    返回当前用户拥有的权限(按模块分组)和角色列表。

    返回字段:
    - roles: 当前用户持有的活跃角色列表(含 scope 信息)
    - permission_groups: 按 module 分组的权限点(含 label,从 Permission 表查询)
    - is_super_admin: 是否超管(系统级快路径)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        u = request.user
        # 角色列表:补充 scope 信息(团队/部门属地授权)
        roles = []
        for ur in u.user_role_rels.select_related("role").filter(status='ACTIVE').all():
            roles.append({
                "id": ur.role.id,
                "code": ur.role.role_key,
                "name": ur.role.name,
                "is_builtin": ur.role.is_builtin,
                "role_type": ur.role.role_type,
                "data_scope": ur.role.data_scope,
                "scope_type": ScopeType.NONE,  # 全局角色无 scope
                "scope_id": None,
                "scope_name": "",
            })
        # 团队属地角色(补 scope)
        for tr in u.team_scope_rels.select_related("role", "team").filter(status='ACTIVE').all():
            roles.append({
                "id": tr.role.id,
                "code": tr.role.role_key,
                "name": tr.role.name,
                "is_builtin": tr.role.is_builtin,
                "role_type": tr.role.role_type,
                "data_scope": tr.role.data_scope,
                "scope_type": ScopeType.TEAM,
                "scope_id": tr.team_id,
                "scope_name": tr.team.name if tr.team else "",
            })
        # 部门属地角色(补 scope)
        for dr in u.dept_scope_rels.select_related("role", "dept").filter(status='ACTIVE').all():
            roles.append({
                "id": dr.role.id,
                "code": dr.role.role_key,
                "name": dr.role.name,
                "is_builtin": dr.role.is_builtin,
                "role_type": dr.role.role_type,
                "data_scope": dr.role.data_scope,
                "scope_type": ScopeType.DEPT,
                "scope_id": dr.dept_id,
                "scope_name": dr.dept.name if dr.dept else "",
            })

        # get_user_permissions(返回 permission_key 集合)
        perm_set = get_user_permissions(u)
        # 批量查询权限点 label(从 Permission 表),避免 N+1
        perm_map = {}
        if perm_set:
            perm_rows = Permission.objects.filter(permission_key__in=perm_set).values(
                'permission_key', 'permission_name', 'module',
            )
            perm_map = {r['permission_key']: r for r in perm_rows}

        # 按模块分组,转换为前端友好的分组结构
        # 新权限点格式为三段式 module.resource.action(如 kb.document.read)
        groups = {}
        for key in perm_set:
            parts = key.split('.')
            module = parts[0] if parts else key
            action = parts[-1] if len(parts) > 1 else ""
            perm_info = perm_map.get(key, {})
            if module not in groups:
                groups[module] = []
            groups[module].append({
                "code": key,
                "action": action,
                "label": perm_info.get('permission_name') or key,
            })
        return Response({
            "roles": roles,
            "permission_groups": groups,
            "is_super_admin": u.is_super_admin,
        })


class PermissionApproversView(APIView):
    """GET /api/v1/auth/permissions/approvers/?scope=team|department|all
    [已废弃] 返回当前用户可选择的审批人列表 —— 新架构由系统自动生成审批链,无需用户选审批人。

    保留此接口仅为向后兼容旧前端,新前端请改用:
    - GET /permissions/assignable-roles/  获取可申请角色
    - GET /permissions/approval-chain-preview/  预览审批链

    旧逻辑(已不推荐):
    - team: 团队 leader + 部门经理
    - department: 部门经理 + 知识库管理员
    - all: 部门经理 + 知识库管理员 + 超级管理员
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        logger.warning(
            f'[Deprecated] PermissionApproversView 被调用(user={request.user.username}),'
            f'请迁移到 approval-chain-preview 接口'
        )
        u = request.user
        scope = (request.query_params.get("scope") or "team").strip()
        approvers = []
        seen_ids = set()

        def _add(user_obj, role_label):
            if not user_obj or user_obj.id in seen_ids or user_obj.id == u.id:
                return
            seen_ids.add(user_obj.id)
            approvers.append({
                "id": user_obj.id,
                "username": user_obj.username,
                "real_name": user_obj.real_name or user_obj.username,
                "email": user_obj.email,
                "role_label": role_label,
            })

        # 用户所属团队的 leader（单团队 FK）
        if scope in ("team", "department", "all"):
            if u.team_id:
                team = Team.objects.filter(id=u.team_id, is_deleted=False).first()
                if team:
                    _add(team.leader, f"{team.name} · 团队负责人")

        # 用户所属部门的 leader
        if scope in ("department", "all"):
            if u.department_id:
                dept = u.department
                _add(dept.leader, f"{dept.name} · 部门经理") if dept else None

        # 知识库管理员（部门及以上级别）
        # 通过 RolePermissionRel 反查拥有 kb.manage_all 权限的活跃用户
        if scope in ("department", "all"):
            kb_admin_users = User.objects.filter(
                is_deleted=False, status=UserStatus.ACTIVE,
                user_role_rels__status='ACTIVE',
                user_role_rels__role__role_permissions__is_active=True,
                user_role_rels__role__role_permissions__permission__permission_key='kb.manage_all',
            ).distinct()
            for k in kb_admin_users:
                _add(k, "知识库管理员")

        # 超级管理员（all 级别）——通过 role_key='super_admin' 反查
        if scope == "all":
            sa_users = User.objects.filter(
                is_deleted=False, status=UserStatus.ACTIVE,
                user_role_rels__status='ACTIVE',
                user_role_rels__role__role_key='super_admin',
            ).distinct()
            for s in sa_users:
                _add(s, "超级管理员")

        return Response({
            "scope": scope,
            "approvers": approvers,
            "count": len(approvers),
        })


class AccessApplicationView(APIView):
    """GET/POST /api/v1/auth/permissions/applications/
    GET: 当前用户的权限申请列表(对齐工单真实字段)
    POST: 提交权限申请(对接 create_ticket 服务,支持 GRANT/REVOKE/ROLE_CHANGE)

    POST 字段(对齐 RBAC 权限架构):
    - role_key:      申请的角色标识(如 viewer/contributor/team_leader/dept_manager 等)
    - scope_type:    管辖范围 TEAM/DEPT/NONE(全局角色填 NONE)
    - scope_id:      scope_type=TEAM 填 team_id;DEPT 填 dept_id;NONE 留空
    - change_type:   GRANT(默认)/REVOKE/ROLE_CHANGE
    - previous_role_id: 仅 ROLE_CHANGE 必填(同 scope 内角色变更,原子撤销旧角色+授予新角色)
    - reason:        申请理由(必填)
    - effective_from/expires_at: 可选,生效/失效时间

    业务规则:
    - 同团队内团队角色(viewer/contributor/team_leader)互斥,高等级覆盖低等级
      → 申请同团队新角色时自动检测已有角色,转为 ROLE_CHANGE(原子覆盖)
    - super_admin 不可自助申请(只能由现有超管发起双人复核工单)
    - viewer 本团队自动授予,不进工单(由节点同步处理,不走本接口)

    参数校验说明:协议层(类型/枚举/必填/参数间约束)由 AccessApplicationSerializer
    完成;业务校验(角色存在性/管理岗任命权限/资源所有者/previous_role 解析)保留在视图。
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """返回当前用户发起的工单列表,字段对齐统一工单(TicketList)真实结构"""
        tickets = TicketList.objects.filter(
            applicant=request.user,
        ).select_related(
            'permission_detail__target_user', 'permission_detail__role',
            'permission_detail__previous_role',
        ).order_by('-created_at')[:100]

        # 批量收集所有审批链中的 approver_id，一次查询解析姓名，避免 N+1
        all_approver_ids = set()
        ticket_list = list(tickets)
        for t in ticket_list:
            for node in (t.approval_chain or []):
                aid = node.get('approver_id')
                if aid:
                    all_approver_ids.add(aid)
        approver_map = {}
        if all_approver_ids:
            approver_map = {
                u.id: (u.real_name or u.username)
                for u in User.objects.filter(id__in=all_approver_ids).only('id', 'real_name', 'username')
            }

        # 批量预加载 scope_name 所需部门/团队，避免循环内逐条查询
        scope_dept_ids = set()
        scope_team_ids = set()
        for t in ticket_list:
            if t.scope_type == ScopeType.DEPT and t.scope_id:
                scope_dept_ids.add(t.scope_id)
            elif t.scope_type == ScopeType.TEAM and t.scope_id:
                scope_team_ids.add(t.scope_id)
        scope_dept_map = {}
        scope_team_map = {}
        if scope_dept_ids:
            scope_dept_map = {
                d.id: d.name for d in Department.objects.filter(
                    id__in=scope_dept_ids, is_deleted=False
                ).only('id', 'name')
            }
        if scope_team_ids:
            scope_team_map = {
                tm.id: tm.name for tm in Team.objects.filter(
                    id__in=scope_team_ids, is_deleted=False
                ).only('id', 'name')
            }

        rows = []
        for t in ticket_list:
            chain = t.approval_chain or []
            # 从预加载的 map 中取 scope 名称（避免逐条查询）
            scope_name = _resolve_scope_name(t.scope_type, t.scope_id, scope_dept_map, scope_team_map)

            # 从预加载的 approver_map 中取审批人姓名（避免逐条查询）
            approver_name = ''
            reviewer_comment = ''
            if t.status in ('APPROVED', 'EXECUTED', 'REJECTED') and chain:
                for node in reversed(chain):
                    if node.get('approver_id'):
                        approver_name = approver_map.get(node['approver_id'], '')
                        if node.get('comment'):
                            reviewer_comment = node['comment']
                        break

            rows.append({
                'id': t.id,
                'ticket_no': t.ticket_no,
                'change_type': t.change_type,
                'status': t.status,
                'role_id': t.role_id,
                'role_key': t.role.role_key if t.role else '',
                'role_name': t.role.name if t.role else '',
                'previous_role_key': t.previous_role.role_key if t.previous_role else '',
                'previous_role_name': t.previous_role.name if t.previous_role else '',
                'scope_type': t.scope_type,
                'scope_id': t.scope_id,
                'scope_name': scope_name,
                'reason': t.reason,
                'approver_name': approver_name,
                'reviewer_comment': reviewer_comment,
                'effective_from': t.effective_from.isoformat() if t.effective_from else '',
                'expires_at': t.expires_at.isoformat() if t.expires_at else '',
                'created_at': t.created_at.isoformat() if t.created_at else '',
                'approved_at': t.approved_at.isoformat() if t.approved_at else '',
                'executed_at': t.executed_at.isoformat() if t.executed_at else '',
                'current_step': t.current_step,
                'total_steps': len(chain),
                'approval_chain': _serialize_chain_nodes(chain),
            })
        return Response({'rows': rows, 'count': len(rows)})

    def post(self, request):
        # ── 协议层参数校验（Serializer）：类型/枚举/必填/参数间约束 ──
        ser = AccessApplicationSerializer(data=request.data)
        if not ser.is_valid():
            # 保持单错误响应契约：返回首条错误文案
            _, detail = _first_serializer_error(ser.errors)
            return Response({'detail': detail}, status=400)
        d = ser.validated_data
        role_key = d['role_key']
        scope_type = d['scope_type']
        scope_id = d.get('scope_id')
        change_type = d['change_type']
        previous_role_id = d.get('previous_role_id')
        reason = d['reason']
        effective_from = d.get('effective_from')
        expires_at = d.get('expires_at')
        target_user_id = d.get('target_user_id')

        # ── 业务校验：角色存在性 ──
        role = Role.objects.filter(role_key=role_key, is_deleted=False).first()
        if not role:
            return Response({'detail': f'角色不存在: {role_key}'}, status=400)

        # ── 管理岗校验:禁止自助申请,仅允许上级发起任命 ──
        # 管理岗(team_leader/dept_manager/kb_admin/compliance_admin/user_admin/super_admin)
        # 必须指定被任命者(target_user_id)且不能是申请人自己,并校验发起人任命权限;
        # 协作角色(viewer/contributor)为资源所有者提单,被授权对象即指定用户。
        if role_key in SELF_APPLY_FORBIDDEN_KEYS:
            if change_type != TicketChangeType.GRANT:
                return Response(
                    {'detail': '管理岗撤销请由管理端处理,本接口仅支持任命(GRANT)'}, status=400,
                )
            if not target_user_id or target_user_id == request.user.id:
                return Response(
                    {'detail': '该角色为管理岗,不能自助申请,请由上级(部门经理/用户管理员)发起任命'},
                    status=403,
                )
            target_user = User.objects.filter(
                id=target_user_id, is_deleted=False, status=UserStatus.ACTIVE,
            ).first()
            if not target_user:
                return Response({'detail': '指定的被任命用户不存在或已禁用'}, status=400)
            if not can_nominate(request.user, role_key, scope_type, scope_id):
                return Response({'detail': '当前用户无权发起该角色的任命工单'}, status=403)
        else:
            # 协作角色(viewer/contributor):资源所有者(组长/部门经理)提单(定稿)
            # 定稿:本团队/本部门其他团队 → 资源团队组长提单自动生效;部门级本部门 → 部门经理提单自动生效;
            # 跨部门团队 → 资源部门经理批准;部门级跨部门 → kb_admin 审核。
            # 不再支持员工自助申请(前端申请入口已下线),统一由资源所有者代提单。
            if change_type not in (
                TicketChangeType.GRANT, TicketChangeType.REVOKE,
                TicketChangeType.ROLE_CHANGE,
            ):
                return Response({'detail': 'change_type 取值应为 GRANT/REVOKE/ROLE_CHANGE'}, status=400)
            if scope_type not in (ScopeType.TEAM, ScopeType.DEPT):
                return Response(
                    {'detail': 'viewer/contributor 必须绑定团队(TEAM)或部门(DEPT)范围'}, status=400,
                )
            if not target_user_id or target_user_id == request.user.id:
                return Response(
                    {'detail': '协作角色授权须由资源团队组长/部门经理指定被授权人提单'},
                    status=403,
                )
            target_user = User.objects.filter(
                id=target_user_id, is_deleted=False, status=UserStatus.ACTIVE,
            ).first()
            if not target_user:
                return Response({'detail': '指定的被授权用户不存在或已禁用'}, status=400)
            if not is_resource_owner(request.user, scope_type, scope_id):
                return Response(
                    {'detail': '当前用户无权提单,协作角色授权须由资源团队组长/部门经理发起'},
                    status=403,
                )

        # ── ROLE_CHANGE previous_role 解析 ──
        # 注:同团队角色互斥检测已下沉到 create_ticket 服务层,
        # 此处仅处理前端显式提交的 ROLE_CHANGE 工单(带 previous_role_id)
        previous_role = None
        if change_type == TicketChangeType.ROLE_CHANGE:
            if not previous_role_id:
                return Response({'detail': 'ROLE_CHANGE 必须提供 previous_role_id'}, status=400)
            previous_role = Role.objects.filter(id=previous_role_id, is_deleted=False).first()
            if not previous_role:
                return Response({'detail': 'previous_role_id 对应角色不存在'}, status=400)

        # ── 管理岗默认有效期 1 年 ──
        # 定稿:管理岗(team_leader/dept_manager/kb_admin/compliance_admin/user_admin/super_admin)
        # 任命时若未指定有效期,默认授予 1 年。到期需重新授权,避免管理权限长期悬挂。
        if (role_key in SELF_APPLY_FORBIDDEN_KEYS
                and change_type == TicketChangeType.GRANT
                and not expires_at):
            expires_at = timezone.now() + datetime.timedelta(days=365)

        # ── 调用工单服务创建审批工单 ──
        # create_ticket 内部会自动检测同团队角色互斥并转为 ROLE_CHANGE
        # 延迟导入：保持测试可 patch apps.users.ticket_service.create_ticket，
        # 避免模块加载时提前绑定函数对象导致 mock 失效
        from apps.users.ticket_service import create_ticket
        ip = _client_ip(request)
        ua = _client_ua(request)
        try:
            ticket = create_ticket(
                applicant=request.user,
                target_user=target_user,  # 资源所有者提单=被授权人;管理岗任命=被任命者
                change_type=change_type,
                role=role,
                scope_type=scope_type,
                scope_id=scope_id,
                effective_from=effective_from,
                expires_at=expires_at,
                reason=reason,
                ip_address=ip,
                user_agent=ua,
                previous_role=previous_role,
            )
        except ValueError as e:
            # SoD 互斥冲突 / 超管硬约束 / 其他业务规则拦截
            return Response({'detail': str(e)}, status=400)
        except Exception as e:
            logger.exception(f'[AccessApplication] 创建工单失败: {e}')
            return Response({'detail': '创建工单失败,请稍后重试'}, status=500)

        logger.info(f'[AccessApplication] 工单创建: id={ticket.id} no={ticket.ticket_no} '
                    f'applicant={request.user.username} role={role_key} '
                    f'change_type={change_type} status={ticket.status}')

        return Response({
            'id': ticket.id,
            'ticket_no': ticket.ticket_no,
            'change_type': ticket.change_type,
            'status': ticket.status,
            'detail': '申请已提交,等待审批' if ticket.status == 'PENDING' else '申请已直接生效',
        }, status=201)


class AccessApplicationWithdrawView(APIView):
    """POST /api/v1/auth/permissions/applications/<id>/withdraw/
    撤回自己的访问申请（仅 pending 状态可撤回）
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            app = TicketList.objects.get(id=pk, applicant=request.user)
        except TicketList.DoesNotExist:
            return Response({"detail": "申请不存在"}, status=404)
        if app.status != TicketStatus.PENDING:
            return Response({"detail": f"当前状态 {app.status} 不可撤回"}, status=400)
        # 走工单服务撤回：置 CANCELLED + 写流转日志 + 审计（保持与工单中心一致）
        cancel_ticket(
            app, request.user,
            ip_address=_client_ip(request),
            user_agent=_client_ua(request),
        )
        return Response({"detail": "已撤回", "status": "cancelled"})


class AssignableRolesView(APIView):
    """GET /api/v1/auth/permissions/assignable-roles/
    返回当前用户可申请的角色清单(按类别分组,含审批链提示)

    业务规则:
    - super_admin 不可自助申请(不返回)
    - viewer/contributor/team_leader:团队级角色,需指定 team_id
    - dept_manager:部门级角色,需指定 dept_id
    - user_admin/kb_admin/compliance_admin:全局高权角色,无需 scope
    - 返回每个角色的审批链概要(审批层级 + 审批人角色),供前端展示

    查询参数:
    - purpose: self(默认,协作角色自助清单) / management(管理岗任命清单)
    - scope_type: 可选,筛选指定范围的角色(TEAM/DEPT/NONE)

    清单组装逻辑已下沉 services/access_service.get_assignable_roles。
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        purpose = (request.query_params.get('purpose') or 'self').strip()
        scope_filter = (request.query_params.get('scope_type') or '').strip()
        rows = get_assignable_roles(purpose, scope_filter)
        return Response({
            'rows': rows,
            'count': len(rows),
        })


class ApprovalChainPreviewView(APIView):
    """GET /api/v1/auth/permissions/approval-chain-preview/
    预览审批链 —— 根据角色 + scope 预生成审批链,供前端在提交前展示

    查询参数:
    - role_key:    申请的角色标识(必填)
    - scope_type:  TEAM/DEPT/NONE(必填)
    - scope_id:    scope_type=TEAM/DEPT 时必填
    - change_type: GRANT(默认)/REVOKE/ROLE_CHANGE

    返回审批链节点列表(含 approver_role 标签 + scope 解析),
    不创建工单,仅用于前端预览展示。

    业务背景:让用户在提交申请前明确知道审批流向,减少无效申请。
    审批链构造与节点/scope 名称解析已下沉 services/access_service.preview_approval_chain。
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        role_key = (request.query_params.get('role_key') or '').strip()
        scope_type = (request.query_params.get('scope_type') or ScopeType.NONE).strip()
        scope_id = request.query_params.get('scope_id')
        change_type = (request.query_params.get('change_type') or TicketChangeType.GRANT).strip()

        # 协议层校验（枚举/必填）留在视图，其余业务校验下沉服务层
        if not role_key:
            return Response({'detail': 'role_key 必填'}, status=400)
        if scope_type not in (ScopeType.TEAM, ScopeType.DEPT, ScopeType.NONE, ScopeType.GLOBAL):
            return Response({'detail': 'scope_type 取值应为 TEAM/DEPT/NONE'}, status=400)

        try:
            nodes, target_scope_name, role_name = preview_approval_chain(
                request.user, role_key, scope_type, scope_id, change_type,
            )
        except ValueError as e:
            return Response({'detail': str(e)}, status=400)

        return Response({
            'role_key': role_key,
            'role_name': role_name,
            'change_type': change_type,
            'scope_type': scope_type,
            'scope_id': int(scope_id) if scope_id else None,
            'scope_name': target_scope_name,
            'chain': nodes,
            'total_steps': len(nodes),
            'is_direct_execute': len(nodes) == 0,
        })
