"""
apps.users.views
- JWT 登录带 last_login_ip / last_login_at 快照写入
- 登录失败落 SecurityIncident 表，供 IP 分析
- Profile / User / Role / Permission / Department 全套 CRUD 端点
- 用户导出/批量导出、搜索筛选、禁用/启用
"""
import csv
import io
import re
from loguru import logger
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.db import models, transaction, IntegrityError
from django.http import HttpResponse
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from pypinyin import pinyin, Style

from apps.users.models import Department, Team, Role, Permission, UserRole, UserTeam, UserCrossScopeAccess, UserScopePermission, has_permission
from apps.users.serializers import (
    UserSerializer, UserCreateSerializer, UserUpdateSerializer,
    DepartmentSerializer, TeamSerializer, CrossScopeAccessSerializer,
    RoleSerializer, PermissionSerializer, ProfileUpdateSerializer,
)

User = get_user_model()


def _auto_code(name, prefix=""):
    """自动生成编码：取拼音首字母，如「研发中心」→ yfzx，组再加部门前缀"""
    py = pinyin(name, style=Style.NORMAL)
    code = ''.join([p[0][0] for p in py if p[0]])
    code = re.sub(r'[^a-z0-9_]', '', code.lower())
    if prefix:
        code = f"{prefix}_{code}"
    return code or 'auto'


def _ensure_unique_code(base_code, model_class, exclude_id=None):
    """确保生成的 code 在表中唯一，冲突时追加数字后缀（单次查询）"""
    import re
    qs = model_class.objects.filter(is_deleted=False)
    if exclude_id:
        qs = qs.exclude(id=exclude_id)
    # 一次查询找出所有 base_code 或 base_code_N 格式的 code
    pattern = rf"^{re.escape(base_code)}(_\d+)?$"
    existing_codes = list(qs.filter(code__iregex=pattern).values_list('code', flat=True))
    
    if not existing_codes:
        return base_code
    
    max_n = 0
    escaped = re.escape(base_code)
    for code in existing_codes:
        if code == base_code:
            max_n = max(max_n, 0)
        m = re.match(rf"^{escaped}_(\d+)$", code)
        if m:
            max_n = max(max_n, int(m.group(1)))
    
    return f"{base_code}_{max_n + 1}"


def _client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _export_users_csv(users_qs, filename="users_export.csv"):
    """将用户 QuerySet 导出为 UTF-8 BOM CSV"""
    buf = io.StringIO()
    buf.write('\ufeff')  # BOM for Excel Chinese support
    writer = csv.writer(buf)
    writer.writerow(["ID", "用户名", "邮箱", "真实姓名", "部门", "状态", "最后登录", "创建时间"])
    for u in users_qs:
        writer.writerow([
            u.id, u.username, u.email, u.real_name,
            u.department.name if u.department else "",
            u.get_status_display() if hasattr(u, "get_status_display") else u.status,
            u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else "",
            u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else "",
        ])
    resp = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


class LoginView(APIView):
    """POST /api/v1/auth/login/  -> {access, refresh, user}"""
    permission_classes = [AllowAny]
    throttle_scope = "login"

    def post(self, request):
        username = (request.data.get("username") or "").strip()
        password = request.data.get("password") or ""
        if not username or not password:
            return Response({"detail": "用户名或密码不能为空"}, status=400)

        user = User.objects.filter(username=username, is_deleted=False).first()
        if not user or not user.check_password(password):
            try:
                from apps.security.models import LoginAttempt
                LoginAttempt.objects.create(
                    username=username[:64],
                    user=user if user else None,
                    ip=_client_ip(request),
                    user_agent=request.META.get("HTTP_USER_AGENT", "")[:256],
                    result="user_not_found" if not user else "wrong_password",
                )
            except Exception:
                logger.exception("write LoginAttempt failed")
            return Response({"detail": "用户名或密码错误"}, status=401)

        if user.status != "active":
            logger.warning(f"Login attempt for inactive user: {user.username}, status={user.status}")
            return Response({"detail": "用户名或密码错误"}, status=401)

        refresh = RefreshToken.for_user(user)
        user.last_login_at = timezone.now()
        user.last_login_ip = _client_ip(request)
        user.save(update_fields=["last_login_at", "last_login_ip"])

        return Response({
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": UserSerializer(user).data,
        })


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = request.data.get("refresh")
        if token:
            try:
                RefreshToken(token).blacklist()
            except Exception:
                pass
        return Response({"ok": True})


class ProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        u = request.user
        # 使用 prefetch_related 预加载关联数据，避免 N+1
        from django.db.models import Prefetch
        user_with_related = User.objects.filter(id=u.id)\
            .select_related('department')\
            .prefetch_related(
                Prefetch('roles', queryset=UserRole.objects.select_related('role')),
                Prefetch('user_teams', queryset=UserTeam.objects.select_related('team')),
                Prefetch('cross_scope_access'),
                Prefetch('scope_permissions'),
            ).first()
        return Response(UserSerializer(user_with_related).data)

    def patch(self, request):
        ser = ProfileUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        u = request.user
        data = ser.validated_data
        for f in ["real_name", "avatar_url", "phone"]:
            if f in data:
                setattr(u, f, data[f])
        # email 不允许用户自行修改（前端 readonly，后端也阻止）
        if "email" in data and data["email"]:
            return Response({"detail": "企业邮箱不可自行修改，请联系管理员"}, status=403)
        u.save()
        return Response(UserSerializer(u).data)


class ResetPasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        old = request.data.get("old_password", "")
        new = request.data.get("new_password", "")
        if len(new) < 8:
            return Response({"detail": "新密码至少 8 位"}, status=400)
        if len(new) > 32:
            return Response({"detail": "新密码最多 32 位"}, status=400)
        if new == old:
            return Response({"detail": "新密码不能与旧密码相同"}, status=400)
        if not re.search(r'[A-Z]', new):
            return Response({"detail": "新密码必须包含大写字母"}, status=400)
        if not re.search(r'[a-z]', new):
            return Response({"detail": "新密码必须包含小写字母"}, status=400)
        if not re.search(r'\d', new):
            return Response({"detail": "新密码必须包含数字"}, status=400)
        if not request.user.check_password(old):
            return Response({"detail": "旧密码错误"}, status=400)
        request.user.set_password(new)
        request.user.password_changed_at = timezone.now()
        request.user.save()
        return Response({"ok": True})


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.filter(is_deleted=False).order_by("-created_at")
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    # ---------- 权限辅助 ----------
    def _get_user_roles(self, user):
        """获取用户的所有角色代码列表"""
        return list(UserRole.objects.filter(user=user).values_list('role__code', flat=True))

    def _check_user_manage(self, target_user=None):
        """检查请求者是否有用户管理权限（及其范围）"""
        u = self.request.user
        # 超级管理员永远放行
        if UserRole.objects.filter(user=u, role__code='super_admin').exists():
            return True
        if not has_permission(u, 'user:manage:all'):
            if target_user:
                # 部门经理只能管理同部门
                if has_permission(u, 'user:manage:department'):
                    if u.department_id and target_user.department_id == u.department_id:
                        return True
                # Team Leader 只能管理同团队
                if has_permission(u, 'user:manage:team'):
                    target_teams = set(
                        UserTeam.objects.filter(user=target_user).values_list('team_id', flat=True)
                    )
                    my_teams = set(
                        UserTeam.objects.filter(user=u).values_list('team_id', flat=True)
                    )
                    if target_teams & my_teams:
                        return True
            return False
        return True

    def _check_can_toggle_status(self, target_user):
        """检查是否可以禁用/启用用户（明确的角色级权限控制）"""
        u = self.request.user
        user_roles = self._get_user_roles(u)
        target_roles = self._get_user_roles(target_user)
        
        # 规则1：只有特定角色才有禁用权限
        allowed_roles = ['super_admin', 'user_admin', 'dept_manager', 'team_leader']
        if not set(user_roles) & set(allowed_roles):
            return False, "没有禁用权限"
        
        # 规则2：不能操作自己
        if u.id == target_user.id:
            return False, "不能禁用自己"
        
        # 规则3：超级管理员不能被禁用
        if 'super_admin' in target_roles:
            return False, "超级管理员不能被禁用"
        
        # 规则4：超级管理员可以禁用除超级管理员以外的所有用户
        if 'super_admin' in user_roles:
            return True, ""
        
        # 规则5：部门经理：只能禁用本部门的员工（排除同级部门经理和user_admin）
        if 'dept_manager' in user_roles and u.department_id:
            if target_user.department_id != u.department_id:
                return False, "只能禁用本部门员工"
            # 不能禁用同级：部门经理、user_admin
            if set(target_roles) & {'dept_manager', 'user_admin'}:
                return False, "不能禁用同级部门经理"
            return True, ""
        
        # 规则6：组长：只能禁用本组（自己领导的团队）的成员（排除同级组长和部门经理）
        if 'team_leader' in user_roles:
            # 使用 Team.leader 精确判断：请求者领导的团队
            my_led_teams = set(Team.objects.filter(leader=u, is_deleted=False).values_list('id', flat=True))
            target_teams = set(UserTeam.objects.filter(user=target_user).values_list('team_id', flat=True))
            if not (my_led_teams & target_teams):
                return False, "只能禁用本组员工"
            # 不能禁用同级：组长、部门经理、user_admin
            if set(target_roles) & {'team_leader', 'dept_manager', 'user_admin'}:
                return False, "不能禁用同级组长"
            return True, ""
        
        # 规则7：user_admin：可以禁用除超级管理员和其他user_admin以外的用户
        if 'user_admin' in user_roles:
            if 'user_admin' in target_roles:
                return False, "不能禁用同级用户管理员"
            return True, ""
        
        return False, "无权限操作"

    def _get_manageable_user_ids(self):
        """获取当前用户可管理的用户ID集合"""
        u = self.request.user
        # 超级管理员可管理所有用户
        if UserRole.objects.filter(user=u, role__code='super_admin').exists():
            return None
        # 部门经理可管理本部门用户
        if has_permission(u, 'user:manage:department') and u.department_id:
            return set(User.objects.filter(department_id=u.department_id, is_deleted=False).values_list('id', flat=True))
        # Team Leader 可管理同团队用户
        if has_permission(u, 'user:manage:team'):
            my_team_ids = list(UserTeam.objects.filter(user=u).values_list('team_id', flat=True))
            return set(UserTeam.objects.filter(team_id__in=my_team_ids).values_list('user_id', flat=True))
        # 普通用户只能管理自己
        return {u.id}

    def _filter_role_ids(self, role_ids):
        """检查角色ID，非超管不能分配高级角色，检测到受限角色时抛出403错误"""
        u = self.request.user
        # 超级管理员可以分配任意角色
        if UserRole.objects.filter(user=u, role__code='super_admin').exists():
            return role_ids
        # 非超管不能分配高级角色
        restricted_roles = ['super_admin', 'kb_admin', 'kb_ops']
        restricted_ids = set(Role.objects.filter(code__in=restricted_roles).values_list('id', flat=True))
        has_restricted = role_ids and restricted_ids & set(role_ids)
        if has_restricted:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("无权分配高级角色")
        return role_ids

    # ---------- 查询（限定范围）----------
    def get_queryset(self):
        qs = super().get_queryset()
        # 添加预加载，减少N+1查询
        qs = qs.select_related('department')\
               .prefetch_related('roles__role', 'user_teams__team',
                                 'cross_scope_access', 'scope_permissions')
        u = self.request.user
        # 超管/知识库运维 -> 看全部
        if UserRole.objects.filter(user=u, role__code__in=['super_admin', 'kb_ops']).exists() or has_permission(u, 'user:manage:all'):
            pass
        elif has_permission(u, 'user:manage:department') and u.department_id:
            qs = qs.filter(department_id=u.department_id)
        elif has_permission(u, 'user:manage:team'):
            my_team_ids = list(UserTeam.objects.filter(user=u).values_list('team_id', flat=True))
            qs = qs.filter(user_teams__team_id__in=my_team_ids).distinct()
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
        # 筛选
        dept_id = self.request.query_params.get("department_id")
        if dept_id:
            qs = qs.filter(department_id=int(dept_id))
        role_id = self.request.query_params.get("role_id")
        if role_id:
            qs = qs.filter(roles__role_id=int(role_id))
        status = self.request.query_params.get("status")
        if status:
            qs = qs.filter(status=status)
        return qs

    def get_serializer_class(self):
        if self.action == "create":
            return UserCreateSerializer
        if self.action in ("update", "partial_update"):
            return UserUpdateSerializer
        return UserSerializer

    # ---- 新建用户 ----
    def create(self, request, *args, **kwargs):
        if not self._check_user_manage():
            return Response({"detail": "无用户管理权限"}, status=403)
        ser = UserCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        role_ids = ser.validated_data.pop("role_ids", [])
        team_ids = ser.validated_data.pop("team_ids", [])
        cross_data = ser.validated_data.pop("cross_scope_access", [])
        scope_perm_data = ser.validated_data.pop("scope_permissions", [])
        pwd = ser.validated_data.pop("password")
        # 过滤角色ID，非超管不能分配高级角色
        role_ids = self._filter_role_ids(role_ids)
        with transaction.atomic():
            user = User.objects.create(**ser.validated_data)
            user.set_password(pwd)
            user.save()
            for rid in role_ids:
                UserRole.objects.get_or_create(user=user, role_id=rid)
            for tid in team_ids:
                if Team.objects.filter(id=tid).exists():
                    UserTeam.objects.get_or_create(user=user, team_id=tid)
            for item in cross_data:
                UserCrossScopeAccess.objects.update_or_create(
                    user=user, scope_type=item.get('scope_type'),
                    scope_id=item.get('scope_id'),
                    defaults={
                        'actions': item.get('actions', 'read'),
                        'granted_by': request.user
                    }
                )
            for item in scope_perm_data:
                UserScopePermission.objects.update_or_create(
                    user=user, scope_type=item.get('scope_type'),
                    scope_id=item.get('scope_id'),
                    defaults={
                        'actions': item.get('actions', 'read'),
                        'granted_by': request.user
                    }
                )
        return Response(UserSerializer(user).data, status=201)

    # ---- 编辑用户 ----
    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if not self._check_user_manage(instance):
            return Response({"detail": "无权限编辑该用户"}, status=403)
        # 检查状态变更权限（所有规则都在这里判断）
        if 'status' in request.data and request.data['status'] != instance.status:
            can_toggle, msg = self._check_can_toggle_status(instance)
            if not can_toggle:
                return Response({"detail": msg}, status=403)
        partial = kwargs.pop("partial", False)
        ser = UserUpdateSerializer(instance, data=request.data, partial=partial)
        ser.is_valid(raise_exception=True)
        role_ids = ser.validated_data.pop("role_ids", None)
        team_ids = ser.validated_data.pop("team_ids", None)
        cross_data = ser.validated_data.pop("cross_scope_access", None)
        scope_perm_data = ser.validated_data.pop("scope_permissions", None)
        user = ser.save()
        # 过滤角色ID，非超管不能分配高级角色
        if role_ids is not None:
            role_ids = self._filter_role_ids(role_ids)
        with transaction.atomic():
            if role_ids is not None:
                UserRole.objects.filter(user=user).delete()
                for rid in role_ids:
                    UserRole.objects.get_or_create(user=user, role_id=rid)
            if team_ids is not None:
                UserTeam.objects.filter(user=user).delete()
                for tid in team_ids:
                    if Team.objects.filter(id=tid).exists():
                        UserTeam.objects.get_or_create(user=user, team_id=tid)
            if cross_data is not None:
                UserCrossScopeAccess.objects.filter(user=user).delete()
                for item in cross_data:
                    UserCrossScopeAccess.objects.get_or_create(
                        user=user, scope_type=item.get('scope_type'),
                        scope_id=item.get('scope_id'),
                        defaults={
                            'actions': item.get('actions', 'read'),
                            'granted_by': request.user
                        }
                    )
            if scope_perm_data is not None:
                UserScopePermission.objects.filter(user=user).delete()
                for item in scope_perm_data:
                    UserScopePermission.objects.get_or_create(
                        user=user, scope_type=item.get('scope_type'),
                        scope_id=item.get('scope_id'),
                        defaults={
                            'actions': item.get('actions', 'read'),
                            'granted_by': request.user
                        }
                    )
        return Response(UserSerializer(user).data)

    # ---- 软删除 ----
    def destroy(self, request, *args, **kwargs):
        u = self.get_object()
        # 检查删除权限（所有规则都在这里判断）
        can_delete, msg = self._check_can_toggle_status(u)
        if not can_delete:
            return Response({"detail": msg}, status=403)
        u.is_deleted = True
        u.deleted_at = timezone.now()
        u.status = "disabled"
        u.save()
        return Response(status=204)

    # ---- 批量删除 ----
    @action(detail=False, methods=["post"])
    def batch_delete(self, request):
        if not self._check_user_manage():
            return Response({"detail": "无用户管理权限"}, status=403)
        ids = request.data.get("ids", [])
        if not ids:
            return Response({"detail": "请选择要删除的用户"}, status=400)
        
        # 逐个检查权限（使用统一的权限判断逻辑）
        valid_ids = []
        targets = list(User.objects.filter(id__in=ids, is_deleted=False))
        for target_user in targets:
            can_delete, msg = self._check_can_toggle_status(target_user)
            if can_delete:
                valid_ids.append(target_user.id)
        
        if not valid_ids:
            return Response({"detail": "所选用户中无可用删除的"}, status=403)
        
        User.objects.filter(id__in=valid_ids, is_deleted=False).update(
            is_deleted=True, deleted_at=timezone.now(), status="disabled"
        )
        return Response({"ok": True, "deleted": len(valid_ids)})

    # ---- 禁用/启用 ----
    @action(detail=True, methods=["post"])
    def toggle_status(self, request, pk=None):
        u = self.get_object()
        # 检查禁用/启用权限（所有规则都在这里判断）
        can_toggle, msg = self._check_can_toggle_status(u)
        if not can_toggle:
            return Response({"detail": msg}, status=403)
        if u.status == "disabled":
            u.status = "active"
        else:
            u.status = "disabled"
        u.save()
        return Response({"id": u.id, "status": u.status})

    # ---- 分配角色（仅超管） ----
    @action(detail=True, methods=["post"])
    def assign_roles(self, request, pk=None):
        if not UserRole.objects.filter(user=request.user, role__code='super_admin').exists():
            return Response({"detail": "仅超级管理员可分配角色"}, status=403)
        u = self.get_object()
        role_ids = request.data.get("role_ids", [])
        with transaction.atomic():
            UserRole.objects.filter(user=u).delete()
            for rid in role_ids:
                UserRole.objects.create(user=u, role_id=rid)
        return Response({"ok": True})

    # ---- 导出单个用户 ----
    @action(detail=True, methods=["get"])
    def export(self, request, pk=None):
        u = self.get_object()
        return _export_users_csv([u], filename=f"user_{u.username}.csv")

    # ---- 批量导出 ----
    @action(detail=False, methods=["post"])
    def batch_export(self, request):
        ids = request.data.get("ids", [])
        if ids:
            # 获取可管理的用户ID集合，过滤掉不在权限范围内的用户
            manageable_ids = self._get_manageable_user_ids()
            if manageable_ids is not None:
                ids = [i for i in ids if i in manageable_ids]
            users = User.objects.filter(id__in=ids, is_deleted=False)
        else:
            users = self.filter_queryset(self.get_queryset())
        return _export_users_csv(users, filename="users_export.csv")

    # ---- 角色、部门、权限点下拉选项 ----
    @action(detail=False, methods=["get"])
    def form_options(self, request):
        depts = list(Department.objects.filter(is_deleted=False).values("id", "name", "code"))
        teams = list(Team.objects.filter(is_deleted=False).values("id", "name", "code", "department_id"))
        # 非超级管理员看不到高级角色
        if UserRole.objects.filter(user=request.user, role__code='super_admin').exists():
            roles = list(Role.objects.all().values("id", "code", "name", "description"))
        else:
            restricted_roles = ['super_admin', 'kb_admin', 'kb_ops']
            roles = list(Role.objects.exclude(code__in=restricted_roles).values("id", "code", "name", "description"))
        permissions = list(Permission.objects.all().values("id", "code", "name", "module", "action", "scope"))
        return Response({"departments": depts, "teams": teams, "roles": roles, "permissions": permissions})


class DepartmentViewSet(viewsets.ModelViewSet):
    queryset = Department.objects.filter(is_deleted=False).order_by("id")
    serializer_class = DepartmentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return super().get_queryset().prefetch_related('team_set')\
            .annotate(user_count=models.Count('users', filter=models.Q(users__is_deleted=False)))

    def _check_super_admin(self):
        if not UserRole.objects.filter(user=self.request.user, role__code='super_admin').exists():
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("仅超级管理员可操作")

    def create(self, request, *args, **kwargs):
        self._check_super_admin()
        data = request.data.copy()
        name = data.get("name", "").strip()
        if not data.get("code", "").strip():
            data["code"] = _auto_code(name)
        # 确保自动生成的 code 唯一
        data["code"] = _ensure_unique_code(data["code"], Department)
        
        # 检查是否已存在同名活跃部门
        if Department.objects.filter(name=name, is_deleted=False).exists():
            return Response({"detail": f"部门\"{name}\"已存在"}, status=400)
        
        deleted_dept = Department.objects.filter(name=name, is_deleted=True).first()
        if deleted_dept:
            # 恢复已删除的部门，但确保 code 不与现存部门冲突
            restored_code = data["code"]
            if Department.objects.filter(code=restored_code, is_deleted=False).exclude(id=deleted_dept.id).exists():
                restored_code = _ensure_unique_code(restored_code, Department)
            deleted_dept.is_deleted = False
            deleted_dept.name = name
            deleted_dept.code = restored_code
            try:
                deleted_dept.save()
            except IntegrityError:
                return Response({"detail": f"部门\"{name}\"已存在"}, status=400)
            logger.info(f"Department.create - restored deleted department: {deleted_dept.name}")
            return Response(DepartmentSerializer(deleted_dept).data, status=201)
        
        ser = self.get_serializer(data=data)
        ser.is_valid(raise_exception=True)
        try:
            self.perform_create(ser)
        except IntegrityError:
            return Response({"detail": f"部门\"{name}\"已存在"}, status=400)
        return Response(ser.data, status=201)

    def update(self, request, *args, **kwargs):
        self._check_super_admin()
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        self._check_super_admin()
        dept = self.get_object()
        user_count = User.objects.filter(department=dept, is_deleted=False).count()
        if user_count > 0:
            return Response({"detail": f"该部门下还有 {user_count} 个用户，无法删除"}, status=400)
        team_count = Team.objects.filter(department=dept, is_deleted=False).count()
        if team_count > 0:
            return Response({"detail": f"该部门下还有 {team_count} 个团队，请先删除或迁移团队"}, status=400)
        dept.is_deleted = True
        dept.save()
        return Response(status=204)


class PermissionViewSet(viewsets.ModelViewSet):
    """权限点 CRUD（仅超级管理员可操作）"""
    queryset = Permission.objects.all().order_by("module", "action", "scope")
    serializer_class = PermissionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return super().get_queryset()

    def create(self, request, *args, **kwargs):
        if not UserRole.objects.filter(user=request.user, role__code='super_admin').exists():
            return Response({"detail": "仅超级管理员可操作"}, status=403)
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        if not UserRole.objects.filter(user=request.user, role__code='super_admin').exists():
            return Response({"detail": "仅超级管理员可操作"}, status=403)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        if not UserRole.objects.filter(user=request.user, role__code='super_admin').exists():
            return Response({"detail": "仅超级管理员可操作"}, status=403)
        perm = self.get_object()
        # 检查是否有角色引用此权限
        from apps.users.models import RolePermission
        ref_count = RolePermission.objects.filter(permission=perm).count()
        if ref_count > 0:
            return Response({"detail": f"该权限点被 {ref_count} 个角色引用，请先解除角色关联"}, status=400)
        perm.delete()
        return Response(status=204)


class RoleViewSet(viewsets.ModelViewSet):
    queryset = Role.objects.all().order_by("id")
    serializer_class = RoleSerializer
    permission_classes = [IsAuthenticated]

    def _check_super_admin(self):
        if not UserRole.objects.filter(user=self.request.user, role__code='super_admin').exists():
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("仅超级管理员可操作")

    def create(self, request, *args, **kwargs):
        self._check_super_admin()
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        self._check_super_admin()
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        self._check_super_admin()
        role = self.get_object()
        if role.is_builtin:
            return Response({"detail": "内置角色不可删除"}, status=400)
        return super().destroy(request, *args, **kwargs)


class TeamViewSet(viewsets.ModelViewSet):
    queryset = Team.objects.filter(is_deleted=False).order_by("id")
    serializer_class = TeamSerializer
    permission_classes = [IsAuthenticated]

    def create(self, request, *args, **kwargs):
        if not UserRole.objects.filter(user=request.user, role__code='super_admin').exists():
            return Response({"detail": "仅超级管理员可操作"}, status=403)
        logger.info(f"Team.create - request user: {request.user.username}, data: {request.data}")
        
        data = dict(request.data)
        name = data.get("name", "").strip()
        dept_id = data.get("department_id")
        
        if not dept_id:
            logger.error(f"Team.create - department_id is required but got: {dept_id}")
            return Response({"detail": "部门ID不能为空"}, status=400)
        
        if isinstance(dept_id, list):
            dept_id = dept_id[0]
        
        dept_id = int(dept_id)
        
        # 一次查询：验证部门存在 + 获取 code 做前缀
        dept = Department.objects.filter(id=dept_id).first()
        if not dept:
            logger.error(f"Team.create - department_id {dept_id} does not exist")
            return Response({"detail": "指定的部门不存在"}, status=400)
        
        data["department_id"] = dept_id
        
        # 检查同部门下是否已存在同名团队
        if Team.objects.filter(name=name, department_id=dept_id, is_deleted=False).exists():
            return Response({"detail": f"部门\"{dept.name}\"下已存在团队\"{name}\""}, status=400)
        
        if not data.get("code", "").strip():
            logger.info(f"Team.create - auto generating code, department_id: {dept_id}")
            prefix = dept.code or _auto_code(dept.name)
            data["code"] = _auto_code(name, prefix)
        # 确保自动生成的 code 唯一
        data["code"] = _ensure_unique_code(data["code"], Team)
        
        deleted_team = Team.objects.filter(name=name, department_id=dept_id, is_deleted=True).first()
        if deleted_team:
            # 恢复已删除的团队，但确保 code 不与现存团队冲突
            restored_code = data["code"]
            if Team.objects.filter(code=restored_code, is_deleted=False).exclude(id=deleted_team.id).exists():
                restored_code = _ensure_unique_code(restored_code, Team)
            deleted_team.is_deleted = False
            deleted_team.name = name
            deleted_team.code = restored_code
            deleted_team.description = data.get("description", "")
            deleted_team.department_id = dept_id
            try:
                deleted_team.save()
            except IntegrityError:
                return Response({"detail": f"部门\"{dept.name}\"下已存在团队\"{name}\""}, status=400)
            logger.info(f"Team.create - restored deleted team: {deleted_team.name}, department_id: {dept_id}")
            return Response(TeamSerializer(deleted_team).data, status=201)
        
        logger.info(f"Team.create - creating new team with data: {data}")
        try:
            team = Team.objects.create(
                name=data["name"],
                code=data["code"],
                department_id=dept_id,
                description=data.get("description")
            )
            logger.info(f"Team.create - success, team id: {team.id}, department_id: {team.department_id}")
            return Response(TeamSerializer(team).data, status=201)
        except IntegrityError:
            return Response({"detail": f"部门\"{dept.name}\"下已存在团队\"{name}\""}, status=400)
        except Exception as e:
            logger.error(f"Team.create - failed, exception: {str(e)}")
            raise

    def update(self, request, *args, **kwargs):
        if not UserRole.objects.filter(user=request.user, role__code='super_admin').exists():
            return Response({"detail": "仅超级管理员可操作"}, status=403)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        if not UserRole.objects.filter(user=request.user, role__code='super_admin').exists():
            return Response({"detail": "仅超级管理员可操作"}, status=403)
        team = self.get_object()
        user_count = UserTeam.objects.filter(team=team).count()
        if user_count > 0:
            return Response({"detail": f"该团队下还有 {user_count} 个用户，无法删除"}, status=400)
        team.is_deleted = True
        team.save()
        return Response(status=204)


# ============================================================================
# 个人权限查看 / 权限申请
# ============================================================================
class MyPermissionsView(APIView):
    """GET /api/v1/auth/permissions/me/
    返回当前用户拥有的权限（按模块分组）和角色列表。
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.users.models import get_user_permission_map
        u = request.user
        roles = [
            {
                "id": ur.role.id,
                "code": ur.role.code,
                "name": ur.role.name,
                "is_builtin": ur.role.is_builtin,
            }
            for ur in u.roles.select_related("role").all()
        ]
        perm_map = get_user_permission_map(u)
        # 转换为前端友好的分组结构
        groups = {}
        for key, scopes in perm_map.items():
            module, action = key.split(":", 1) if ":" in key else (key, "")
            if module not in groups:
                groups[module] = []
            groups[module].append({
                "code": f"{module}:{action}:{scopes[0]}" if scopes else key,
                "action": action,
                "scopes": scopes,
                "label": f"{action} / {','.join(scopes)}",
            })
        return Response({
            "roles": roles,
            "permission_groups": groups,
            "is_super_admin": UserRole.objects.filter(user=u, role__code='super_admin').exists(),
        })


class PermissionApproversView(APIView):
    """GET /api/v1/auth/permissions/approvers/?scope=team|department|all
    返回当前用户可选择的审批人列表。
    - team: 团队 leader + 部门经理
    - department: 部门经理 + 知识库运维(kb_ops)
    - all: 部门经理 + 知识库运维 + 超级管理员
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
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

        # 用户所属团队的 leader
        if scope in ("team", "department", "all"):
            team_ids = list(UserTeam.objects.filter(user=u).values_list("team_id", flat=True))
            for t in Team.objects.filter(id__in=team_ids, is_deleted=False):
                _add(t.leader, f"{t.name} · 团队负责人")

        # 用户所属部门的 leader
        if scope in ("department", "all"):
            if u.department_id:
                dept = u.department
                _add(dept.leader, f"{dept.name} · 部门经理") if dept else None

        # 知识库运维 + 超级管理员（部门及以上级别）
        if scope in ("department", "all"):
            kb_ops_users = User.objects.filter(
                roles__role__code__in=["kb_ops"], is_deleted=False, status="active"
            ).distinct()
            for k in kb_ops_users:
                _add(k, "知识库运维")

        if scope == "all":
            sa_users = User.objects.filter(
                roles__role__code="super_admin", is_deleted=False, status="active"
            ).distinct()
            for s in sa_users:
                _add(s, "超级管理员")

        return Response({
            "scope": scope,
            "approvers": approvers,
            "count": len(approvers),
        })


class PermissionApplicationView(APIView):
    """GET/POST /api/v1/auth/permissions/applications/
    GET: 当前用户的申请列表
    POST: 提交新的权限申请
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.users.models import PermissionApplication
        apps = PermissionApplication.objects.filter(applicant=request.user).order_by("-created_at")[:50]
        rows = []
        for a in apps:
            rows.append({
                "id": a.id,
                "permission_code": a.permission_code or (a.permission.code if a.permission else ""),
                "permission_name": a.permission.name if a.permission else a.permission_code,
                "applied_scope": a.applied_scope,
                "reason": a.reason,
                "approver_id": a.approver_id,
                "approver_name": (a.approver.real_name or a.approver.username) if a.approver else "—",
                "status": a.status,
                "reviewer_comment": a.reviewer_comment,
                "created_at": a.created_at.isoformat() if a.created_at else "",
                "reviewed_at": a.reviewed_at.isoformat() if a.reviewed_at else "",
            })
        return Response({"rows": rows, "count": len(rows)})

    def post(self, request):
        from apps.users.models import PermissionApplication
        perm_code = (request.data.get("permission_code") or "").strip()
        perm_id = request.data.get("permission_id")
        applied_scope = (request.data.get("applied_scope") or "team").strip()
        reason = (request.data.get("reason") or "").strip()
        approver_id = request.data.get("approver_id")

        if not (perm_code or perm_id):
            return Response({"detail": "permission_code 或 permission_id 必填"}, status=400)
        if applied_scope not in ("team", "department", "all"):
            return Response({"detail": "applied_scope 取值应为 team/department/all"}, status=400)
        if not approver_id:
            return Response({"detail": "请选择审批人"}, status=400)
        if not reason:
            return Response({"detail": "请填写申请理由"}, status=400)

        # 校验审批人是否存在
        try:
            approver = User.objects.get(id=approver_id, is_deleted=False, status="active")
        except User.DoesNotExist:
            return Response({"detail": "审批人不存在或已禁用"}, status=400)

        perm_obj = None
        if perm_id:
            try:
                perm_obj = Permission.objects.get(id=perm_id)
            except Permission.DoesNotExist:
                pass
        if not perm_obj and perm_code:
            try:
                perm_obj = Permission.objects.get(code=perm_code)
            except Permission.DoesNotExist:
                pass

        app = PermissionApplication.objects.create(
            applicant=request.user,
            permission=perm_obj,
            permission_code=perm_code or (perm_obj.code if perm_obj else ""),
            applied_scope=applied_scope,
            reason=reason,
            approver=approver,
            status="pending",
        )
        logger.info(f"PermissionApplication created: id={app.id}, applicant={request.user.username}, "
                    f"perm={perm_code}, scope={applied_scope}, approver={approver.username}")
        return Response({
            "id": app.id,
            "detail": "申请已提交，等待审批",
            "status": "pending",
        }, status=201)


class PermissionApplicationWithdrawView(APIView):
    """POST /api/v1/auth/permissions/applications/<id>/withdraw/
    撤回自己的权限申请（仅 pending 状态可撤回）
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        from apps.users.models import PermissionApplication
        try:
            app = PermissionApplication.objects.get(id=pk, applicant=request.user)
        except PermissionApplication.DoesNotExist:
            return Response({"detail": "申请不存在"}, status=404)
        if app.status != "pending":
            return Response({"detail": f"当前状态 {app.status} 不可撤回"}, status=400)
        app.status = "withdrawn"
        app.save(update_fields=["status", "updated_at"])
        return Response({"detail": "已撤回", "status": "withdrawn"})
