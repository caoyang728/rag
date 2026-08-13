"""用户管理视图：UserViewSet（CRUD / 批量导入导出 / 权限详情 / 角色分配 / 搜索筛选 / 禁用启用）

权限判定、角色分配校验、软删除、批量导入等业务逻辑已下沉至 services/user_service，
本文件保留 DRF 编排（序列化、响应、HTTP 参数解析）。
"""
import csv
import io
import secrets
import string

from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.http import HttpResponse
from django.utils import timezone

from rest_framework import viewsets, serializers
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.users.models import (
    Department, Team, Role,
    UserRoleRel, UserDeptScopeRel, UserTeamScopeRel,
    has_permission, get_user_data_scope_level,
    get_user_managed_depts, get_user_managed_teams, DataScope,
    UserStatus,
)
from apps.users.permissions import CanManageUsers
from apps.users.serializers import (
    UserSerializer, UserListSerializer, UserCreateSerializer, UserUpdateSerializer,
)
from apps.users.services.user_service import (
    check_user_manage, check_can_manage_user, get_manageable_user_ids,
    filter_downward_roles, filter_role_ids, validate_role_uniqueness,
    soft_delete, sync_role_leader, import_users_batch,
    get_user_manage_scope,
)
from apps.users.utils import _export_users_csv, _sanitize_csv_field

User = get_user_model()


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.filter(is_deleted=False).order_by("-created_at")
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated, CanManageUsers]
    # 允许排序的字段（DRF OrderingFilter 自动识别 ordering 查询参数）
    ordering_fields = ['username', 'email', 'real_name', 'created_at', 'last_login_at']
    ordering = '-created_at'

    # ---------- 查询（限定范围）----------
    def get_queryset(self):
        qs = super().get_queryset()
        # 仅对需要展示关联数据的 action 做预加载（list / retrieve / search），
        # create / update / destroy 等写操作不需要 prefetch，减少无效查询
        if self.action in ('list', 'retrieve', 'search', 'form_options', 'export',
                           'batch_export', 'permission_detail'):
            # 添加预加载，减少N+1查询：
            # - department/team: 单 FK select_related
            # - user_role_rels__role: 全局角色关联
            # - dept_scope_rels / team_scope_rels: 部门/团队属地授权（get_roles 补入 dept_manager/team_leader）
            qs = qs.select_related('department', 'team')\
                   .prefetch_related(
                       'user_role_rels__role',
                       'dept_scope_rels__role',
                       'team_scope_rels__role',
                   )
        u = self.request.user
        # 拥有 user.manage_all 权限 -> 看全部（RBAC）
        if u.is_super_admin or has_permission(u, 'user.manage_all'):
            pass
        elif has_permission(u, 'user.manage'):
            u_scope = get_user_data_scope_level(u)
            # 部门级：可见本部门（含属地授权部门）用户
            if u_scope == DataScope.DEPT:
                managed_depts = get_user_managed_depts(u)
                qs = qs.filter(department_id__in=managed_depts)
            # 团队级：可见本团队（含属地授权团队）用户
            elif u_scope == DataScope.TEAM:
                managed_teams = get_user_managed_teams(u)
                qs = qs.filter(team_id__in=managed_teams)
            else:
                # 普通员工/只读员工：只能看到自己
                qs = qs.filter(id=u.id)
        else:
            # 普通员工/只读员工：只能看到自己
            qs = qs.filter(id=u.id)

        # 搜索
        search = self.request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(
                models.Q(username__icontains=search)
                | models.Q(email__icontains=search)
                | models.Q(real_name__icontains=search)
            )
        # 筛选（安全转换，防止恶意非数字参数导致 500）
        dept_id = self.request.query_params.get("department_id")
        if dept_id:
            try:
                qs = qs.filter(department_id=int(dept_id))
            except (ValueError, TypeError):
                pass
        team_id = self.request.query_params.get("team_id")
        if team_id:
            try:
                # 单团队 FK：直接 filter team_id
                qs = qs.filter(team_id=int(team_id))
            except (ValueError, TypeError):
                pass
        role_id = self.request.query_params.get("role_id")
        if role_id:
            try:
                # 仅统计活跃授权（status='ACTIVE'），避免已撤销角色的用户被误纳入筛选结果；
                # distinct 去重同一用户持有多条角色关联（含历史 REVOKED 记录）产生的重复行
                qs = qs.filter(user_role_rels__role_id=int(role_id),
                               user_role_rels__status='ACTIVE').distinct()
            except (ValueError, TypeError):
                pass
        # 校验 status 参数合法性，避免恶意值绕过过滤或污染查询
        status = self.request.query_params.get("status")
        if status:
            valid_statuses = {c[0] for c in UserStatus.choices}
            if status in valid_statuses:
                qs = qs.filter(status=status)
        return qs

    def get_serializer_class(self):
        if self.action == "list":
            return UserListSerializer
        if self.action == "create":
            return UserCreateSerializer
        if self.action in ("update", "partial_update"):
            return UserUpdateSerializer
        return UserSerializer

    # ---- 新建用户 ----
    def create(self, request, *args, **kwargs):
        u = request.user
        # 获取管理层级标识（超管/全局管理/部门管理/团队管理）
        scope = get_user_manage_scope(u)
        is_super = scope['is_super']
        can_manage_all = scope['can_manage_all']
        is_dept = scope['is_dept']
        is_team = scope['is_team']
        if not is_super and not can_manage_all and not is_dept and not is_team:
            return Response({"detail": "无用户管理权限"}, status=403)
        ser = UserCreateSerializer(data=request.data)
        # EAFP：99% 场景无冲突，直接走校验；命中唯一冲突时再判断是否为"邮箱命中已删除用户"
        # —— 避免每个新建都多做一次 SELECT 预检测
        try:
            ser.is_valid(raise_exception=True)
        except serializers.ValidationError as exc:
            # 仅 email 字段的唯一错误需要额外判断"是否命中已删除用户 → 走恢复"
            # username 冲突（"具有 username 的 user 已存在"）按规则不提供恢复，直接报错
            email_errors = exc.detail.get("email") if isinstance(exc.detail, dict) else None
            if email_errors:
                email_value = request.data.get("email")
                if email_value:
                    revivable = User.objects.filter(
                        email__iexact=email_value.strip(), is_deleted=True,
                    ).first()
                    if revivable:
                        return Response(
                            {
                                "detail": "该邮箱曾属于已删除用户，是否恢复原账号？",
                                "code": "USER_REVIVABLE",
                                "revivable_user": {
                                    "id": revivable.id,
                                    "username": revivable.username,
                                    "real_name": revivable.real_name,
                                    "deleted_at": (
                                        revivable.deleted_at.isoformat()
                                        if revivable.deleted_at else None
                                    ),
                                },
                            },
                            status=409,
                        )
            raise
        role_ids = ser.validated_data.pop("role_ids", [])
        team_ids = ser.validated_data.pop("team_ids", [])
        department_id = ser.validated_data.pop("department_id", None)
        # 过滤角色ID，非超管不能分配高级角色；组长只能分配组长/普通员工
        role_ids = filter_role_ids(
            u, role_ids if (is_super or can_manage_all) else filter_downward_roles(role_ids, is_dept)
        )
        # 组长/部门经理自动锁定部门/团队
        if is_team:
            department_id = u.department_id
            if not team_ids:
                # 单团队 FK：组长默认分配到自己所属团队
                team_ids = [u.team_id] if u.team_id else []
        elif is_dept:
            # 部门经理只能在本部门（含属地授权部门）内创建用户，防止越权建到其他部门
            if department_id and department_id not in get_user_managed_depts(u):
                return Response({"detail": "无权在该部门创建用户"}, status=403)
            department_id = department_id or u.department_id
        # 组长只能分配到自己可管理的团队
        if is_team and team_ids:
            my_teams = get_user_managed_teams(u)
            invalid = set(team_ids) - my_teams
            if invalid:
                return Response({"detail": "只能分配到自己的团队"}, status=403)
        # 部门经理（非组长）只能把用户分配到本部门（含属地授权部门）下的团队
        elif is_dept and team_ids:
            my_teams = get_user_managed_teams(u)
            invalid = set(team_ids) - my_teams
            if invalid:
                return Response({"detail": "只能分配到本部门下的团队"}, status=403)
        pwd = ser.validated_data.pop("password", "") or ""
        if not pwd:
            # 使用加密安全随机数生成默认密码，避免可预测性攻击
            alphabet = string.ascii_letters + string.digits + "!@#$"
            pwd = ''.join(secrets.choice(alphabet) for _ in range(12))
        # 校验部门经理/团队leader唯一性
        conflict = validate_role_uniqueness(None, role_ids, department_id, team_ids)
        if conflict:
            return Response({"detail": conflict}, status=400)
        with transaction.atomic():
            user = User.objects.create(**ser.validated_data)
            # department_id 需与密码一并持久化：save(update_fields) 只写指定字段，
            # 否则部门赋值丢失(此前组长建人/带部门建人均不落库)
            if department_id is not None:
                user.department_id = department_id
            user.set_password(pwd)
            save_fields = ['password']
            if department_id is not None:
                save_fields.append('department_id')
            user.save(update_fields=save_fields)
            # 批量创建角色关联
            if role_ids:
                objs = [UserRoleRel(user=user, role_id=rid, status='ACTIVE', granted_by=u) for rid in role_ids]
                UserRoleRel.objects.bulk_create(
                    objs,
                    update_conflicts=True,
                    update_fields=['status', 'revoked_at', 'revoked_by'],
                    unique_fields=['user_id', 'role_id']
                )
            # 单团队 FK：取第一个 team_id（兼容前端传入数组）
            if team_ids:
                new_team_id = team_ids[0]
                if Team.objects.filter(id=new_team_id, is_deleted=False).exists():
                    user.team_id = new_team_id
                    user.save(update_fields=['team', 'updated_at'])
            # 同步 Team.leader_id / Department.leader_id
            if role_ids:
                sync_role_leader(user, set(role_ids))
        return Response(UserSerializer(user).data, status=201)

    # ---- 编辑用户 ----
    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if not check_user_manage(request.user, instance):
            return Response({"detail": "无权限编辑该用户"}, status=403)
        # 检查状态变更权限（所有规则都在这里判断）
        if 'status' in request.data and request.data['status'] != instance.status:
            can_toggle, msg = check_can_manage_user(request.user, instance)
            if not can_toggle:
                return Response({"detail": msg}, status=403)
        partial = kwargs.pop("partial", False)
        ser = UserUpdateSerializer(instance, data=request.data, partial=partial)
        ser.is_valid(raise_exception=True)
        role_ids = ser.validated_data.pop("role_ids", None)
        team_ids = ser.validated_data.pop("team_ids", None)
        # 权限范围校验：防止组长/部门经理越权提权或移动用户
        u = request.user
        scope = get_user_manage_scope(u)
        has_team = scope['is_team']
        has_dept = scope['is_dept']
        is_super = scope['is_super']
        if not is_super:
            if not has_dept and not has_team:
                return Response({"detail": "无权限编辑该用户"}, status=403)
            # 部门经理/组长不能提权：过滤可分配角色
            if role_ids is not None:
                role_ids = filter_downward_roles(role_ids, is_dept=has_dept)
            # 组长（含双重角色）：只能操作自己的团队
            if has_team:
                my_teams = get_user_managed_teams(u)
                if team_ids is not None:
                    if not team_ids:
                        return Response({"detail": "不能清空所有团队"}, status=403)
                    if set(team_ids) - my_teams:
                        return Response({"detail": "只能分配到自己的团队"}, status=403)
                if 'department_id' in request.data:
                    return Response({"detail": "无权修改部门"}, status=403)
            # 部门经理（非组长）：只能操作自己部门
            elif has_dept:
                if 'department_id' in request.data:
                    new_dept_id = request.data['department_id']
                    if isinstance(new_dept_id, str) and new_dept_id == '':
                        new_dept_id = None
                    if new_dept_id != u.department_id:
                        return Response({"detail": "无权修改部门"}, status=403)
                # 部门经理只能把用户分配到本部门（含属地授权部门）下的团队，防止越权跨部门分配
                if team_ids is not None:
                    my_teams = get_user_managed_teams(u)
                    if set(team_ids) - my_teams:
                        return Response({"detail": "只能分配到本部门下的团队"}, status=403)
        # 过滤角色ID，非超管不能分配高级角色（防御层）
        if role_ids is not None:
            role_ids = filter_role_ids(u, role_ids)
        # 校验部门经理/团队leader唯一性 —— 必须在写库之前，避免部分更新已写入
        if role_ids is not None or 'department_id' in request.data or team_ids is not None:
            check_role_ids = role_ids if role_ids is not None else list(
                UserRoleRel.objects.filter(user=instance, status='ACTIVE').values_list('role_id', flat=True)
            )
            actual_dept_id = ser.validated_data.get('department_id') if 'department_id' in ser.validated_data else instance.department_id
            if isinstance(actual_dept_id, str) and actual_dept_id == '':
                actual_dept_id = None
            conflict = validate_role_uniqueness(instance, check_role_ids, actual_dept_id, team_ids)
            if conflict:
                return Response({"detail": conflict}, status=400)
        # 团队存在性校验也提前到事务外：事务内 return 会提交已写入的角色变更，导致部分更新
        new_team_id = None
        if team_ids is not None:
            # 单团队 FK：取第一个 team_id（兼容前端传入数组）
            new_team_id = team_ids[0] if team_ids else None
            if new_team_id and not Team.objects.filter(id=new_team_id, is_deleted=False).exists():
                return Response({"detail": "指定的团队不存在"}, status=400)
        # ser.save() 与角色/团队变更同处一个事务：任一步失败整体回滚，避免资料已存而角色未变的部分更新
        with transaction.atomic():
            user = ser.save()
            if role_ids is not None:
                self._apply_role_change(user, role_ids, request.user)
            if team_ids is not None:
                user.team_id = new_team_id
                user.save(update_fields=['team', 'updated_at'])
        return Response(UserSerializer(user).data)

    # ---- 软删除 ----
    def destroy(self, request, *args, **kwargs):
        u = self.get_object()
        # 检查删除权限（所有规则都在这里判断）
        can_delete, msg = check_can_manage_user(request.user, u)
        if not can_delete:
            return Response({"detail": msg}, status=403)
        soft_delete(u)
        return Response(status=204)

    # ---- 批量删除 ----
    @action(detail=False, methods=["post"])
    def batch_delete(self, request):
        if not check_user_manage(request.user):
            return Response({"detail": "无用户管理权限"}, status=403)
        ids = request.data.get("ids", [])
        if not ids:
            return Response({"detail": "请选择要删除的用户"}, status=400)

        # 逐个检查权限（使用统一的权限判断逻辑）
        # 用 set 存储可删除 ID，O(1) 查找（批量场景下避免 list 的 O(n) 线性扫描）
        valid_ids = set()
        targets = list(User.objects.filter(id__in=ids, is_deleted=False))
        for target_user in targets:
            can_delete, msg = check_can_manage_user(request.user, target_user)
            if can_delete:
                valid_ids.add(target_user.id)

        if not valid_ids:
            return Response({"detail": "所选用户中无可用删除的"}, status=403)

        for target_user in targets:
            if target_user.id in valid_ids:
                soft_delete(target_user)
        return Response({"ok": True, "deleted": len(valid_ids)})

    # ---- 禁用/启用 ----
    @action(detail=True, methods=["post"])
    def toggle_status(self, request, pk=None):
        u = self.get_object()
        # 检查禁用/启用权限（所有规则都在这里判断）
        can_toggle, msg = check_can_manage_user(request.user, u)
        if not can_toggle:
            return Response({"detail": msg}, status=403)
        if u.status == UserStatus.DISABLED:
            u.status = UserStatus.ACTIVE
        else:
            u.status = UserStatus.DISABLED
        u.save(update_fields=['status', 'updated_at'])
        return Response({"id": u.id, "status": u.status})

    # ---- 恢复已软删除用户 ----
    @action(detail=True, methods=["post"])
    def revive(self, request, pk=None):
        """恢复软删除用户：清除删除标记，清空原显式角色重新按 viewer 兜底
        业务背景：同邮箱视为同一人，恢复后保留原审计历史，但权限需重新申请
        """
        # 直接从全量数据中查询（包括已软删除），避免被默认 queryset 过滤掉
        u = User.objects.filter(pk=pk).first()
        if not u or not u.is_deleted:
            # 统一返回 404，避免区分"不存在"和"未删除"被用于用户枚举
            return Response({"detail": "用户不存在或未处于删除状态"}, status=404)
        # 权限校验：复用删除权限，能删就能恢复
        can_revive, msg = check_can_manage_user(request.user, u)
        if not can_revive:
            return Response({"detail": msg}, status=403)
        # 恢复字段：可选覆盖 real_name/department_id/team_id/status
        real_name = request.data.get("real_name") or u.real_name
        department_id = request.data.get("department_id", u.department_id)
        team_ids = request.data.get("team_ids") or []
        status = request.data.get("status", UserStatus.ACTIVE)
        with transaction.atomic():
            u.is_deleted = False
            u.deleted_at = None
            u.status = status
            u.real_name = real_name
            u.department_id = department_id
            # 单团队 FK：取第一个 team_id
            u.team_id = team_ids[0] if team_ids else None
            u.save(update_fields=['is_deleted', 'deleted_at', 'status', 'real_name',
                                  'department_id', 'team', 'updated_at'])
            # 清空原显式授权（global/dept/team 三张表），避免离职前权限直接复活
            UserRoleRel.objects.filter(user=u).delete()
            UserDeptScopeRel.objects.filter(user=u).delete()
            UserTeamScopeRel.objects.filter(user=u).delete()
            # 重新分配 viewer 兜底（写入 global 表，与新建用户一致）
            viewer_role = Role.objects.filter(role_key='viewer').first()
            if viewer_role:
                UserRoleRel.objects.create(
                    user=u, role=viewer_role,
                    status='ACTIVE', granted_by=request.user,
                )
        return Response(UserSerializer(u).data)

    # ---- 用户权限详情（弹窗用）----
    @action(detail=True, methods=["get"], url_path='permission-detail')
    def permission_detail(self, request, pk=None):
        """返回用户权限的扁平行列表，用于弹窗表格展示
        每行：部门-团队-权限-截至日期
        人事归属团队默认只读(viewer/永久)，有显式团队授权则替换
        部门级授权显示为"全部团队"，全局角色显示为"全部/全部"
        """
        # 使用 self.get_object() 而非直接 User.objects.get(pk=pk)，
        # 确保走 get_queryset() 的权限范围过滤（团队组长/部门经理只能查看管辖范围内用户）
        u = self.get_object()
        from apps.users.models import _active_grant_filter

        active_q = _active_grant_filter()
        rows = []

        # 1) 团队级授权：先查显式授权，人事归属团队无显式授权时补 viewer 兜底
        team_rels = list(
            UserTeamScopeRel.objects.filter(active_q, user=u)
            .select_related('role', 'team', 'team__department')
            .values('role__role_key', 'role__name',
                    'team__id', 'team__name',
                    'team__department__id', 'team__department__name',
                    'effective_from', 'expires_at')
        )
        # 按 team_id 索引，方便人事归属团队查找替换
        team_grant_map = {}
        for tr in team_rels:
            row = {
                'dept_name': tr['team__department__name'] or '—',
                'team_name': tr['team__name'] or '—',
                'role_name': tr['role__name'],
                'role_code': tr['role__role_key'],
                'effective_from': tr['effective_from'].date().isoformat() if tr['effective_from'] else None,
                'expires_at': tr['expires_at'].date().isoformat() if tr['expires_at'] else None,
            }
            team_grant_map[tr['team__id']] = row
            rows.append(row)

        # 人事归属团队：无显式授权时补 viewer 兜底
        hr_dept_name = u.department.name if (u.department and not u.department.is_deleted) else None
        hr_team_name = u.team.name if (u.team and not u.team.is_deleted) else None
        if u.team_id and u.team and not u.team.is_deleted:
            if u.team_id not in team_grant_map:
                rows.insert(0, {
                    'dept_name': hr_dept_name or '—',
                    'team_name': hr_team_name or '—',
                    'role_name': '查看者',
                    'role_code': 'viewer',
                    'effective_from': None,
                    'expires_at': None,
                })

        # 2) 部门级授权：显示为"全部团队"
        dept_rels = list(
            UserDeptScopeRel.objects.filter(active_q, user=u)
            .select_related('role', 'dept')
            .values('role__role_key', 'role__name', 'dept__name',
                    'effective_from', 'expires_at')
        )
        for dr in dept_rels:
            rows.append({
                'dept_name': dr['dept__name'] or '—',
                'team_name': '全部团队',
                'role_name': dr['role__name'],
                'role_code': dr['role__role_key'],
                'effective_from': dr['effective_from'].date().isoformat() if dr['effective_from'] else None,
                'expires_at': dr['expires_at'].date().isoformat() if dr['expires_at'] else None,
            })

        # 3) 全局角色：显示为"全部/全部"，跳过 viewer（已在兜底中处理）
        global_rels = list(
            UserRoleRel.objects.filter(active_q, user=u)
            .select_related('role')
            .values('role__role_key', 'role__name', 'effective_from', 'expires_at')
        )
        for gr in global_rels:
            if gr['role__role_key'] == 'viewer':
                continue
            rows.append({
                'dept_name': '全部',
                'team_name': '全部',
                'role_name': gr['role__name'],
                'role_code': gr['role__role_key'],
                'effective_from': gr['effective_from'].date().isoformat() if gr['effective_from'] else None,
                'expires_at': gr['expires_at'].date().isoformat() if gr['expires_at'] else None,
            })

        return Response({
            'user': {
                'id': u.id,
                'username': u.username,
                'real_name': u.real_name or u.username,
            },
            'hr_dept_name': hr_dept_name,
            'hr_team_name': hr_team_name,
            'rows': rows,
        })

    # ---- 角色分配核心逻辑（update / assign_roles 共用） ----
    def _apply_role_change(self, user, role_ids, operator):
        """全量覆盖用户角色：撤销不再保留的 + 批量写入新角色 + 同步 leader

        供 update（编辑用户）和 assign_roles（超管分配）共用，避免两处维护同一套逻辑。
        """
        # 撤销不再保留的角色（status='REVOKED'）
        UserRoleRel.objects.filter(
            user=user,
            status='ACTIVE',
        ).exclude(role_id__in=role_ids).update(
            status='REVOKED',
            revoked_at=timezone.now(),
            revoked_by=operator,
        )

        if role_ids:
            objs = [UserRoleRel(user=user, role_id=rid, status='ACTIVE', granted_by=operator) for rid in role_ids]
            UserRoleRel.objects.bulk_create(
                objs,
                update_conflicts=True,
                update_fields=['status', 'revoked_at', 'revoked_by'],
                unique_fields=['user_id', 'role_id'],
            )

        sync_role_leader(user, set(role_ids))

    # ---- 分配角色（仅超管） ----
    @action(detail=True, methods=["post"])
    def assign_roles(self, request, pk=None):
        if not request.user.is_super_admin:
            return Response({"detail": "仅超级管理员可分配角色"}, status=403)
        u = self.get_object()
        role_ids = request.data.get("role_ids", [])
        with transaction.atomic():
            self._apply_role_change(u, role_ids, request.user)
        return Response({"ok": True})

    # ---- 导出单个用户 ----
    @action(detail=True, methods=["get"])
    def export(self, request, pk=None):
        u = self.get_object()
        # 传 QuerySet 而非单对象：_export_users_csv 内部需 select_related，
        # 传 list 会抛 AttributeError 导致 500（batch_export 已传 QuerySet，属正常路径）
        return _export_users_csv(User.objects.filter(id=u.id), filename=f"user_{u.username}.csv")

    # ---- 批量导出 ----
    @action(detail=False, methods=["post"])
    def batch_export(self, request):
        ids = request.data.get("ids", [])
        if ids:
            # 获取可管理的用户ID集合，过滤掉不在权限范围内的用户
            manageable_ids = get_manageable_user_ids(request.user)
            if manageable_ids is not None:
                ids = [i for i in ids if i in manageable_ids]
            users = User.objects.filter(id__in=ids, is_deleted=False)
        else:
            users = self.filter_queryset(self.get_queryset())
        return _export_users_csv(users, filename="users_export.csv")

    # ---- 批量导入 ----
    @action(detail=False, methods=["post"])
    def batch_import(self, request):
        """POST /api/v1/auth/users/batch_import/
        上传 CSV 文件批量导入员工，返回带「结果」和「原因」两列的 CSV 供下载。
        CSV 列：用户名, 姓名, 邮箱, 部门, 团队, 状态
        导入用户默认 viewer 角色，不支持通过 CSV 指定角色
        """
        u = request.user
        # 获取管理层级标识（超管/全局管理/部门管理/团队管理），与 create 一致
        scope = get_user_manage_scope(u)
        is_super = scope['is_super']
        can_manage_all = scope['can_manage_all']
        is_dept = scope['is_dept']
        is_team = scope['is_team']
        if not is_super and not can_manage_all and not is_dept and not is_team:
            return Response({"detail": "无用户管理权限"}, status=403)

        csv_file = request.FILES.get("file")
        if not csv_file:
            return Response({"detail": "请上传 CSV 文件"}, status=400)
        if not csv_file.name.lower().endswith(".csv"):
            return Response({"detail": "仅支持 .csv 文件"}, status=400)

        # 读取文件内容并处理 BOM（Excel 中文 CSV 通常带 UTF-8 BOM）
        raw = csv_file.read().decode("utf-8-sig")
        reader = csv.reader(io.StringIO(raw))
        rows = list(reader)
        if not rows:
            return Response({"detail": "CSV 文件为空"}, status=400)

        # 解析表头，建立列名→索引映射，兼容列顺序变化
        header = [h.strip() for h in rows[0]]
        col_map = {}
        for idx, name in enumerate(header):
            col_map[name] = idx

        # 校验必填列
        required_cols = ["用户名", "姓名"]
        missing = [c for c in required_cols if c not in col_map]
        if missing:
            return Response({"detail": f"CSV 缺少必要列：{','.join(missing)}"}, status=400)

        # 预加载部门、团队映射（按名称查找，减少 N+1 查询）
        dept_map = {d.name: d for d in Department.objects.filter(is_deleted=False)}
        team_map = {(t.name, t.department_id): t for t in Team.objects.filter(is_deleted=False)}

        # 导入用户默认 viewer 角色
        viewer_role = Role.objects.filter(role_key='viewer').first()

        # 组长锁定部门/团队，与 create 逻辑一致
        my_team_ids = list(get_user_managed_teams(u)) if is_team else []

        # 逐行校验并创建（下沉至 user_service，行级逻辑与 create 保持一致）
        success_count, fail_count, out_rows = import_users_batch(
            rows[1:], col_map, u, is_dept, is_team,
            dept_map, team_map, viewer_role, my_team_ids,
        )

        # 输出 CSV：原列 + 结果 + 原因
        out_buf = io.StringIO()
        out_buf.write('\ufeff')  # BOM for Excel
        writer = csv.writer(out_buf)
        writer.writerow(header + ["结果", "原因"])
        for row in out_rows:
            # 对每个字段做 CSV 注入防护，防止恶意公式被执行
            writer.writerow([_sanitize_csv_field(cell) for cell in row])

        # 返回带结果的 CSV 文件
        resp = HttpResponse(out_buf.getvalue(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="users_import_result.csv"'
        # 通过自定义 header 返回统计信息，前端可读取展示 toast
        resp["X-Import-Success"] = str(success_count)
        resp["X-Import-Fail"] = str(fail_count)
        return resp

    # ---- 下载导入模板 ----
    @action(detail=False, methods=["get"])
    def import_template(self, request):
        """GET /api/v1/auth/users/import_template/
        下载 CSV 导入模板，含表头和示例行。
        """
        buf = io.StringIO()
        buf.write('\ufeff')
        writer = csv.writer(buf)
        writer.writerow(["用户名", "姓名", "邮箱", "部门", "团队", "状态"])
        writer.writerow(["zhangsan", "张三", "zhangsan@example.com", "研发部", "后端组", "启用"])
        writer.writerow(["lisi", "李四", "lisi@example.com", "研发部", "前端组", "启用"])
        writer.writerow(["wangwu", "王五", "王五@example.com", "", "", "禁用"])
        resp = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="users_import_template.csv"'
        return resp

    # ---- 角色、部门下拉选项 ----
    @action(detail=False, methods=["get"])
    def form_options(self, request):
        depts = list(Department.objects.filter(is_deleted=False).values("id", "name", "code"))
        teams = list(Team.objects.filter(is_deleted=False).values("id", "name", "code", "department_id"))
        # 根据当前用户权限过滤可分配的角色（基于 permission_key 判定，清除角色硬编码）
        u = request.user
        # 辅助函数：annotate code=F('role_key') 保持 API 字段名 code 不变
        def _role_values(qs):
            return list(qs.annotate(code=models.F("role_key")).values("id", "code", "name", "description"))

        if u.is_super_admin:
            roles = _role_values(Role.objects.all())
            assignable = roles
        elif has_permission(u, 'kb.manage_all'):
            # 知识库管理员：除 super_admin 外全可见
            roles = _role_values(Role.objects.exclude(role_key='super_admin'))
            assignable = roles
        elif has_permission(u, 'user.manage'):
            u_scope = get_user_data_scope_level(u)
            if u_scope == DataScope.DEPT:
                # 部门经理：可见普通角色，可分配 team_leader/contributor/viewer
                roles = _role_values(Role.objects.exclude(role_key__in=['super_admin', 'user_admin']))
                assignable = _role_values(Role.objects.filter(role_key__in=['team_leader', 'contributor', 'viewer']))
            else:
                # 团队组长：可见 team_leader/contributor/viewer，仅可分配 contributor/viewer
                roles = _role_values(Role.objects.filter(role_key__in=['team_leader', 'contributor', 'viewer']))
                assignable = _role_values(Role.objects.filter(role_key__in=['contributor', 'viewer']))
        else:
            roles = []
            assignable = []
        return Response({"departments": depts, "teams": teams, "roles": roles, "assignable_roles": assignable})

    # ---- 用户搜索（用于部门经理/团队leader选择） ----
    @action(detail=False, methods=["get"])
    def search(self, request):
        """搜索用户，用于部门经理/团队leader选择框"""
        q = (request.query_params.get("q") or "").strip()
        dept_id = request.query_params.get("department_id")
        team_id = request.query_params.get("team_id")
        qs = User.objects.filter(is_deleted=False, status=UserStatus.ACTIVE)
        # 按管理范围过滤：非超管只能搜索其管理范围内的用户
        manageable_ids = get_manageable_user_ids(request.user)
        if manageable_ids is not None:
            qs = qs.filter(id__in=manageable_ids)
        if q:
            qs = qs.filter(
                models.Q(username__icontains=q)
                | models.Q(real_name__icontains=q)
                | models.Q(email__icontains=q)
            )
        if dept_id:
            try:
                qs = qs.filter(department_id=int(dept_id))
            except (ValueError, TypeError):
                pass
        if team_id:
            # 单团队 FK：直接 filter team_id
            try:
                qs = qs.filter(team_id=int(team_id))
            except (ValueError, TypeError):
                pass
        users = list(qs[:20].values("id", "username", "real_name", "email", "department_id"))
        return Response({"users": users})
