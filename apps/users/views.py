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
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from pypinyin import pinyin, Style

from apps.users.models import Department, Team, Role, Permission, UserRole, UserTeam, has_permission
from apps.users.permissions import CanManageUsers
from apps.users.serializers import (
    UserSerializer, UserCreateSerializer, UserUpdateSerializer,
    DepartmentSerializer, DepartmentWriteSerializer, TeamSerializer, TeamWriteSerializer,
    RoleSerializer, PermissionSerializer, ProfileUpdateSerializer,
)

User = get_user_model()


def _auto_code(name, prefix=""):
    """自动生成编码：取拼音首字母，如「研发部」→ yfb，组再加部门前缀"""
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
    writer.writerow(["ID", "用户名", "邮箱", "真实姓名", "部门", "团队", "角色", "状态", "最后登录", "创建时间"])
    for u in users_qs.select_related('department').prefetch_related('roles__role', 'user_teams__team'):
        team_names = ', '.join(t.team.name for t in u.user_teams.all() if not t.team.is_deleted) or ''
        role_names = ', '.join(r.role.name for r in u.roles.all()) or ''
        writer.writerow([
            u.id, u.username, u.email, u.real_name,
            u.department.name if u.department else "",
            team_names,
            role_names,
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
        captcha_id = request.data.get("captcha_id") or ""
        captcha_code = request.data.get("captcha_code") or ""
        
        if not username or not password:
            return Response({"detail": "用户名或密码不能为空"}, status=400)

        # 验证码验证
        from apps.security.views import verify_captcha
        if not verify_captcha(captcha_id, captcha_code):
            try:
                from apps.security.models import LoginAttempt
                LoginAttempt.objects.create(
                    username=username[:64],
                    user=None,
                    ip=_client_ip(request),
                    user_agent=request.META.get("HTTP_USER_AGENT", "")[:256],
                    result="captcha_fail",
                )
            except Exception:
                logger.exception("write LoginAttempt failed")
            return Response({"detail": "验证码错误"}, status=401)

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

        # 记录成功登录
        try:
            from apps.security.models import LoginAttempt
            LoginAttempt.objects.create(
                username=username[:64],
                user=user,
                ip=_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:256],
                result="success",
            )
        except Exception:
            logger.exception("write LoginAttempt failed")

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
    permission_classes = [IsAuthenticated, CanManageUsers]

    # ---------- 权限辅助 ----------
    def _get_user_roles(self, user):
        """获取用户的所有角色代码列表（带请求级缓存）"""
        if not hasattr(self, '_role_cache'):
            self._role_cache = {}
        if user.id not in self._role_cache:
            self._role_cache[user.id] = list(UserRole.objects.filter(user=user).values_list('role__code', flat=True))
        return self._role_cache[user.id]

    def _check_user_manage(self, target_user=None):
        """检查请求者是否有用户管理权限（及其范围）"""
        u = self.request.user
        # 超级管理员永远放行
        if UserRole.objects.filter(user=u, role__code='super_admin').exists():
            return True
        if not has_permission(u, 'user:manage_users:all'):
            if target_user:
                # 部门经理只能管理同部门
                if has_permission(u, 'user:manage_users:department'):
                    if u.department_id and target_user.department_id == u.department_id:
                        return True
                # 团队组长只能管理同团队
                if has_permission(u, 'user:manage_users:team'):
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

    def _check_can_manage_user(self, target_user):
        """检查是否可以禁用/启用/删除用户（统一的用户操作权限控制）"""
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
        
        # 规则6：团队组长：只能禁用同组成员（排除同级团队组长和部门经理）
        if 'team_leader' in user_roles:
            my_team_ids = set(UserTeam.objects.filter(user=u).values_list('team_id', flat=True))
            target_teams = set(UserTeam.objects.filter(user=target_user).values_list('team_id', flat=True))
            if not (my_team_ids & target_teams):
                return False, "只能禁用本组员工"
            # 不能禁用同级：团队组长、部门经理、user_admin
            if set(target_roles) & {'team_leader', 'dept_manager', 'user_admin'}:
                return False, "不能禁用同级团队组长"
            return True, ""
        
        # 规则7：user_admin：可以禁用除超级管理员和其他user_admin以外的用户
        if 'user_admin' in user_roles:
            if set(target_roles) & {'super_admin', 'user_admin'}:
                return False, "不能禁用同级用户管理员"
            return True, ""
        
        return False, "无权限操作"

    def _get_manageable_user_ids(self):
        """获取当前用户可管理的用户ID集合"""
        u = self.request.user
        # 超管/user_admin/kb_admin/kb_ops 可管理所有用户
        if UserRole.objects.filter(user=u, role__code__in=['super_admin', 'user_admin', 'kb_admin', 'kb_ops']).exists():
            return None
        # 部门经理可管理本部门用户
        if has_permission(u, 'user:manage_users:department') and u.department_id:
            return set(User.objects.filter(department_id=u.department_id, is_deleted=False).values_list('id', flat=True))
        # 团队组长可管理同团队用户
        if has_permission(u, 'user:manage_users:team'):
            my_team_ids = list(UserTeam.objects.filter(user=u).values_list('team_id', flat=True))
            return set(UserTeam.objects.filter(team_id__in=my_team_ids).values_list('user_id', flat=True))
        # 普通用户只能管理自己
        return {u.id}

    def _filter_downward_roles(self, role_ids, is_dept):
        """组长只能分配普通员工/只读员工；部门经理只能分配组长/员工/只读（不能分配其他部门经理）"""
        allowed_codes = ['employee', 'readonly']
        if is_dept:
            allowed_codes = ['team_leader', 'employee', 'readonly']
        allowed_ids = set(Role.objects.filter(code__in=allowed_codes).values_list('id', flat=True))
        return [rid for rid in (role_ids or []) if rid in allowed_ids]

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
            raise PermissionDenied("无权分配高级角色")
        return role_ids

    def _validate_role_uniqueness(self, user, role_ids, department_id=None, team_ids=None):
        """校验 dept_manager 和 team_leader 的唯一性约束"""
        if not role_ids:
            return None
        # 批量查询所有 role_id 对应的 code，避免 N+1
        role_map = dict(Role.objects.filter(id__in=role_ids).values_list('id', 'code'))
        dept_manager_role_id = None
        team_leader_role_id = None
        for rid, code in role_map.items():
            if code == 'dept_manager':
                dept_manager_role_id = rid
            if code == 'team_leader':
                team_leader_role_id = rid

        # 部门经理唯一性：同一部门只能有一个部门经理
        if dept_manager_role_id and department_id:
            existing = UserRole.objects.filter(
                role__code='dept_manager'
            ).exclude(user=user).filter(
                user__department_id=department_id,
                user__is_deleted=False
            ).first()
            if existing:
                return f"该部门已有部门经理：{existing.user.real_name or existing.user.username}"

        # 团队 leader 唯一性：同一团队只能有一个 team_leader
        if team_leader_role_id and team_ids:
            for tid in team_ids:
                existing = UserRole.objects.filter(
                    role__code='team_leader'
                ).exclude(user=user).filter(
                    user__user_teams__team_id=tid,
                    user__is_deleted=False
                ).first()
                if existing:
                    team_name = Team.objects.filter(id=tid).values_list('name', flat=True).first() or f'团队#{tid}'
                    return f"团队\"{team_name}\"已有团队组长：{existing.user.real_name or existing.user.username}"
        return None

    # ---------- 查询（限定范围）----------
    def get_queryset(self):
        qs = super().get_queryset()
        # 添加预加载，减少N+1查询
        qs = qs.select_related('department')\
               .prefetch_related('roles__role', 'user_teams__team')
        u = self.request.user
        # 超管/user_admin/kb_admin/kb_ops 或拥有 user:manage_users:all 权限 -> 看全部
        if UserRole.objects.filter(user=u, role__code__in=['super_admin', 'user_admin', 'kb_admin', 'kb_ops']).exists() \
                or has_permission(u, 'user:manage_users:all'):
            pass
        elif has_permission(u, 'user:manage_users:department') and u.department_id:
            qs = qs.filter(department_id=u.department_id)
        elif has_permission(u, 'user:manage_users:team'):
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
                qs = qs.filter(user_teams__team_id=int(team_id)).distinct()
            except (ValueError, TypeError):
                pass
        role_id = self.request.query_params.get("role_id")
        if role_id:
            try:
                qs = qs.filter(roles__role_id=int(role_id))
            except (ValueError, TypeError):
                pass
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
        u = request.user
        # 超管或拥有 user:manage_users:all 权限直接放行
        is_super = UserRole.objects.filter(user=u, role__code='super_admin').exists()
        can_manage_all = has_permission(u, 'user:manage_users:all')
        is_dept = not is_super and not can_manage_all and has_permission(u, 'user:manage_users:department')
        is_team = not is_super and not can_manage_all and not is_dept and has_permission(u, 'user:manage_users:team')
        if not is_super and not can_manage_all and not is_dept and not is_team:
            return Response({"detail": "无用户管理权限"}, status=403)
        ser = UserCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        role_ids = ser.validated_data.pop("role_ids", [])
        team_ids = ser.validated_data.pop("team_ids", [])
        department_id = ser.validated_data.pop("department_id", None)
        # 过滤角色ID，非超管不能分配高级角色；组长只能分配组长/员工/只读
        role_ids = self._filter_role_ids(
            role_ids if (is_super or can_manage_all) else self._filter_downward_roles(role_ids, is_dept)
        )
        # 组长/部门经理自动锁定部门/团队
        if is_team:
            department_id = u.department_id
            if not team_ids:
                team_ids = list(UserTeam.objects.filter(user=u).values_list('team_id', flat=True))
        elif is_dept:
            department_id = department_id or u.department_id
        # 组长只能分配到自己所在的团队
        if is_team and team_ids:
            my_teams = set(UserTeam.objects.filter(user=u).values_list('team_id', flat=True))
            invalid = set(team_ids) - my_teams
            if invalid:
                return Response({"detail": "只能分配到自己的团队"}, status=403)
        pwd = ser.validated_data.pop("password", "") or ""
        if not pwd:
            username = ser.validated_data.get("username", "")
            pwd = username[:1].upper() + username[1:].lower() + "@1234"
        # 校验部门经理/团队leader唯一性
        conflict = self._validate_role_uniqueness(None, role_ids, department_id, team_ids)
        if conflict:
            return Response({"detail": conflict}, status=400)
        with transaction.atomic():
            user = User.objects.create(**ser.validated_data)
            if department_id is not None:
                user.department_id = department_id
            user.set_password(pwd)
            user.save()
            # 批量创建角色关联
            if role_ids:
                existing_ur = set(UserRole.objects.filter(user=user).values_list('role_id', flat=True))
                new_roles = [UserRole(user=user, role_id=rid) for rid in role_ids if rid not in existing_ur]
                if new_roles:
                    UserRole.objects.bulk_create(new_roles)
            # 批量创建团队关联
            if team_ids:
                existing_ut = set(UserTeam.objects.filter(user=user).values_list('team_id', flat=True))
                valid_teams = [tid for tid in team_ids if Team.objects.filter(id=tid).exists() and tid not in existing_ut]
                if valid_teams:
                    UserTeam.objects.bulk_create([UserTeam(user=user, team_id=tid) for tid in valid_teams])
        return Response(UserSerializer(user).data, status=201)

    # ---- 编辑用户 ----
    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if not self._check_user_manage(instance):
            return Response({"detail": "无权限编辑该用户"}, status=403)
        # 检查状态变更权限（所有规则都在这里判断）
        if 'status' in request.data and request.data['status'] != instance.status:
            can_toggle, msg = self._check_can_manage_user(instance)
            if not can_toggle:
                return Response({"detail": msg}, status=403)
        partial = kwargs.pop("partial", False)
        ser = UserUpdateSerializer(instance, data=request.data, partial=partial)
        ser.is_valid(raise_exception=True)
        role_ids = ser.validated_data.pop("role_ids", None)
        team_ids = ser.validated_data.pop("team_ids", None)
        # 权限范围校验：防止组长/部门经理越权提权或移动用户
        u = request.user
        has_team = has_permission(u, 'user:manage_users:team')
        has_dept = has_permission(u, 'user:manage_users:department')
        is_super = UserRole.objects.filter(user=u, role__code='super_admin').exists()
        if not is_super:
            if not has_dept and not has_team:
                return Response({"detail": "无权限编辑该用户"}, status=403)
            # 部门经理/组长不能提权：过滤可分配角色
            if role_ids is not None:
                role_ids = self._filter_downward_roles(role_ids, is_dept=has_dept)
            # 组长（含双重角色）：只能操作自己的团队
            if has_team:
                my_teams = set(UserTeam.objects.filter(user=u).values_list('team_id', flat=True))
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
        # 过滤角色ID，非超管不能分配高级角色（防御层）
        if role_ids is not None:
            role_ids = self._filter_role_ids(role_ids)
        # 校验部门经理/团队leader唯一性 —— 必须在 ser.save() 之前，避免部分更新已写入
        if role_ids is not None or 'department_id' in request.data or team_ids is not None:
            check_role_ids = role_ids if role_ids is not None else list(
                UserRole.objects.filter(user=instance).values_list('role_id', flat=True)
            )
            actual_dept_id = ser.validated_data.get('department_id') if 'department_id' in ser.validated_data else instance.department_id
            if isinstance(actual_dept_id, str) and actual_dept_id == '':
                actual_dept_id = None
            conflict = self._validate_role_uniqueness(instance, check_role_ids, actual_dept_id, team_ids)
            if conflict:
                return Response({"detail": conflict}, status=400)
        user = ser.save()
        with transaction.atomic():
            if role_ids is not None:
                old_role_ids = set(UserRole.objects.filter(user=user).values_list('role_id', flat=True))
                new_role_ids = set(role_ids)
                # 删除不再需要的角色
                to_remove = old_role_ids - new_role_ids
                if to_remove:
                    UserRole.objects.filter(user=user, role_id__in=to_remove).delete()
                # 添加新角色
                to_add = new_role_ids - old_role_ids
                if to_add:
                    UserRole.objects.bulk_create([UserRole(user=user, role_id=rid) for rid in to_add])
            if team_ids is not None:
                old_team_ids = set(UserTeam.objects.filter(user=user).values_list('team_id', flat=True))
                new_team_ids = set(team_ids)
                # 删除不再需要的团队
                to_remove = old_team_ids - new_team_ids
                if to_remove:
                    UserTeam.objects.filter(user=user, team_id__in=to_remove).delete()
                # 添加新团队
                valid_to_add = [tid for tid in (new_team_ids - old_team_ids) if Team.objects.filter(id=tid).exists()]
                if valid_to_add:
                    UserTeam.objects.bulk_create([UserTeam(user=user, team_id=tid) for tid in valid_to_add])
        return Response(UserSerializer(user).data)

    # ---- 软删除 ----
    def destroy(self, request, *args, **kwargs):
        u = self.get_object()
        # 检查删除权限（所有规则都在这里判断）
        can_delete, msg = self._check_can_manage_user(u)
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
            can_delete, msg = self._check_can_manage_user(target_user)
            if can_delete:
                valid_ids.append(target_user.id)
        
        if not valid_ids:
            return Response({"detail": "所选用户中无可用删除的"}, status=403)
        
        now = timezone.now()
        for target_user in targets:
            if target_user.id in valid_ids:
                target_user.is_deleted = True
                target_user.deleted_at = now
                target_user.status = "disabled"
                target_user.save()
        return Response({"ok": True, "deleted": len(valid_ids)})

    # ---- 禁用/启用 ----
    @action(detail=True, methods=["post"])
    def toggle_status(self, request, pk=None):
        u = self.get_object()
        # 检查禁用/启用权限（所有规则都在这里判断）
        can_toggle, msg = self._check_can_manage_user(u)
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

    # ---- 角色、部门下拉选项 ----
    @action(detail=False, methods=["get"])
    def form_options(self, request):
        depts = list(Department.objects.filter(is_deleted=False).values("id", "name", "code"))
        teams = list(Team.objects.filter(is_deleted=False).values("id", "name", "code", "department_id"))
        # 根据当前用户角色过滤可分配的角色
        u = request.user
        user_codes = list(UserRole.objects.filter(user=u).values_list('role__code', flat=True))
        if 'super_admin' in user_codes:
            roles = list(Role.objects.all().values("id", "code", "name", "description"))
            assignable = roles
        elif 'kb_admin' in user_codes:
            roles = list(Role.objects.exclude(code='super_admin').values("id", "code", "name", "description"))
            assignable = roles
        elif 'kb_ops' in user_codes or 'dept_manager' in user_codes:
            excluded = ['super_admin', 'kb_admin', 'kb_ops']
            roles = list(Role.objects.exclude(code__in=excluded).values("id", "code", "name", "description"))
            # 部门经理不能分配其他部门经理
            assignable = list(Role.objects.filter(code__in=['team_leader', 'employee', 'readonly'])
                              .values("id", "code", "name", "description"))
        elif 'team_leader' in user_codes:
            roles = list(Role.objects.filter(code__in=['team_leader', 'employee', 'readonly'])
                         .values("id", "code", "name", "description"))
            # 组长不能分配其他组长，仅能分配普通/只读员工
            assignable = list(Role.objects.filter(code__in=['employee', 'readonly'])
                              .values("id", "code", "name", "description"))
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
        qs = User.objects.filter(is_deleted=False, status='active')
        # 按管理范围过滤：非超管只能搜索其管理范围内的用户
        manageable_ids = self._get_manageable_user_ids()
        if manageable_ids is not None:
            qs = qs.filter(id__in=manageable_ids)
        if q:
            qs = qs.filter(
                models.Q(username__icontains=q)
                | models.Q(real_name__icontains=q)
                | models.Q(email__icontains=q)
            )
        if dept_id:
            qs = qs.filter(department_id=int(dept_id))
        if team_id:
            qs = qs.filter(user_teams__team_id=int(team_id))
        users = list(qs[:20].values("id", "username", "real_name", "email", "department_id"))
        return Response({"users": users})


class DepartmentViewSet(viewsets.ModelViewSet):
    queryset = Department.objects.filter(is_deleted=False).order_by("id")\
        .select_related('leader')
    serializer_class = DepartmentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        from django.db.models import Prefetch
        return super().get_queryset().prefetch_related(
            Prefetch('team_set', queryset=Team.objects.filter(is_deleted=False).select_related('leader'))
        ).annotate(user_count=models.Count('users', filter=models.Q(users__is_deleted=False)))

    def _check_can_manage_dept(self):
        """检查是否有部门管理权限：超级管理员 或 用户管理员(kb_admin)"""
        u = self.request.user
        if UserRole.objects.filter(user=u, role__code__in=['super_admin', 'kb_admin']).exists():
            return True
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("仅超级管理员和用户管理员可操作")

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return DepartmentWriteSerializer
        return DepartmentSerializer

    def _set_leader(self, dept, leader_id):
        """设置部门经理，如果 leader_id 为 None 则清除"""
        if leader_id is not None:
            leader = User.objects.filter(id=leader_id, is_deleted=False, status='active').first()
            if not leader:
                return False, "指定的用户不存在或已禁用"
            dept.leader = leader
        else:
            dept.leader = None
        dept.save(update_fields=['leader', 'updated_at'])
        return True, ""

    def create(self, request, *args, **kwargs):
        self._check_can_manage_dept()
        data = request.data.copy()
        name = data.get("name", "").strip()
        leader_id = data.get("leader_id")
        # 清理非模型字段
        data.pop("leader_id", None)
        if not data.get("code", "").strip():
            data["code"] = _auto_code(name)
        data["code"] = _ensure_unique_code(data["code"], Department)

        if Department.objects.filter(name=name, is_deleted=False).exists():
            return Response({"detail": f"部门\"{name}\"已存在"}, status=400)

        deleted_dept = Department.objects.filter(name=name, is_deleted=True).first()
        if deleted_dept:
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
            if leader_id is not None:
                self._set_leader(deleted_dept, leader_id)
            logger.info(f"Department.create - restored deleted department: {deleted_dept.name}")
            return Response(DepartmentSerializer(deleted_dept).data, status=201)

        ser = DepartmentWriteSerializer(data=data)
        ser.is_valid(raise_exception=True)
        try:
            dept = ser.save()
        except IntegrityError:
            return Response({"detail": f"部门\"{name}\"已存在"}, status=400)
        if leader_id is not None:
            self._set_leader(dept, leader_id)
        return Response(DepartmentSerializer(dept).data, status=201)

    def update(self, request, *args, **kwargs):
        self._check_can_manage_dept()
        dept = self.get_object()
        leader_id = request.data.get("leader_id")
        data = {k: v for k, v in request.data.items() if k != 'leader_id'}
        ser = DepartmentWriteSerializer(dept, data=data, partial=kwargs.get('partial', False))
        ser.is_valid(raise_exception=True)
        try:
            dept = ser.save()
        except IntegrityError:
            return Response({"detail": "部门编码冲突"}, status=400)
        if 'leader_id' in request.data:
            ok, msg = self._set_leader(dept, leader_id)
            if not ok:
                return Response({"detail": msg}, status=400)
        return Response(DepartmentSerializer(dept).data)

    def destroy(self, request, *args, **kwargs):
        self._check_can_manage_dept()
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

    def _check_super_admin(self):
        if not UserRole.objects.filter(user=self.request.user, role__code='super_admin').exists():
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("仅超级管理员可操作")

    def create(self, request, *args, **kwargs):
        self._check_super_admin()
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        self._check_super_admin()
        perm = self.get_object()
        # 内置权限（code 以 "user:" 或 "system:" 或 "audit:" 开头）不允许修改其核心字段
        if perm.code and (perm.code.startswith('user:') or perm.code.startswith('system:')):
            forbidden = [k for k in request.data if k not in ('description', 'name')]
            if forbidden:
                return Response({"detail": "内置系统权限不允许修改核心字段"}, status=403)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        self._check_super_admin()
        perm = self.get_object()
        # 内置权限不可删除
        if perm.code and (perm.code.startswith('user:manage') or perm.code.startswith('system:')):
            return Response({"detail": "内置系统权限不允许删除"}, status=403)
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
    queryset = Team.objects.filter(is_deleted=False).order_by("id")\
        .select_related('leader', 'department')
    serializer_class = TeamSerializer
    permission_classes = [IsAuthenticated]

    def _check_can_manage_team(self, dept_id=None):
        """检查是否有团队管理权限：超级管理员、用户管理员(kb_admin)、或所属部门经理"""
        u = self.request.user
        if UserRole.objects.filter(user=u, role__code__in=['super_admin', 'kb_admin']).exists():
            return True
        # 部门经理只能管理自己部门的团队
        if UserRole.objects.filter(user=u, role__code='dept_manager').exists():
            if dept_id is None:
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied("部门经理仅可操作本部门团队")
            manager_depts = set(Department.objects.filter(leader=u, is_deleted=False).values_list('id', flat=True))
            if dept_id in manager_depts:
                return True
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("无权限操作团队")

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return TeamWriteSerializer
        return TeamSerializer

    def _set_leader(self, team, leader_id):
        """设置团队 leader，如果 leader_id 为 None 则清除"""
        if leader_id is not None:
            leader = User.objects.filter(id=leader_id, is_deleted=False, status='active').first()
            if not leader:
                return False, "指定的用户不存在或已禁用"
            team.leader = leader
        else:
            team.leader = None
        team.save(update_fields=['leader', 'updated_at'])
        return True, ""

    def create(self, request, *args, **kwargs):
        logger.info(f"Team.create - request user: {request.user.username}, data: {request.data}")

        data = dict(request.data)
        name = data.get("name", "").strip()
        dept_id = data.get("department_id")
        leader_id = data.get("leader_id")
        data.pop("leader_id", None)

        if not dept_id:
            logger.error(f"Team.create - department_id is required but got: {dept_id}")
            return Response({"detail": "部门ID不能为空"}, status=400)

        if isinstance(dept_id, list):
            dept_id = dept_id[0]

        dept_id = int(dept_id)
        self._check_can_manage_team(dept_id)

        dept = Department.objects.filter(id=dept_id).first()
        if not dept:
            logger.error(f"Team.create - department_id {dept_id} does not exist")
            return Response({"detail": "指定的部门不存在"}, status=400)

        data["department_id"] = dept_id

        if Team.objects.filter(name=name, department_id=dept_id, is_deleted=False).exists():
            return Response({"detail": f"部门\"{dept.name}\"下已存在团队\"{name}\""}, status=400)

        if not data.get("code", "").strip():
            logger.info(f"Team.create - auto generating code, department_id: {dept_id}")
            prefix = dept.code or _auto_code(dept.name)
            data["code"] = _auto_code(name, prefix)
        data["code"] = _ensure_unique_code(data["code"], Team)

        deleted_team = Team.objects.filter(name=name, department_id=dept_id, is_deleted=True).first()
        if deleted_team:
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
            if leader_id is not None:
                self._set_leader(deleted_team, leader_id)
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
            if leader_id is not None:
                self._set_leader(team, leader_id)
            logger.info(f"Team.create - success, team id: {team.id}, department_id: {team.department_id}")
            return Response(TeamSerializer(team).data, status=201)
        except IntegrityError:
            return Response({"detail": f"部门\"{dept.name}\"下已存在团队\"{name}\""}, status=400)
        except Exception as e:
            logger.error(f"Team.create - failed, exception: {str(e)}")
            raise

    def update(self, request, *args, **kwargs):
        team = self.get_object()
        self._check_can_manage_team(team.department_id)
        leader_id = request.data.get("leader_id")
        data = {k: v for k, v in request.data.items() if k != 'leader_id'}
        ser = TeamWriteSerializer(team, data=data, partial=kwargs.get('partial', False))
        ser.is_valid(raise_exception=True)
        try:
            team = ser.save()
        except IntegrityError:
            return Response({"detail": "团队编码冲突"}, status=400)
        if 'leader_id' in request.data:
            ok, msg = self._set_leader(team, leader_id)
            if not ok:
                return Response({"detail": msg}, status=400)
        return Response(TeamSerializer(team).data)

    def destroy(self, request, *args, **kwargs):
        team = self.get_object()
        self._check_can_manage_team(team.department_id)
        user_count = UserTeam.objects.filter(team=team).count()
        if user_count > 0:
            return Response({"detail": f"该团队下还有 {user_count} 个成员，无法删除"}, status=400)

        # 检查团队节点及子孙分类节点下是否有文档
        from apps.knowledge.models import KnowledgeNode
        from apps.knowledge.node_sync import count_docs_in_subtree
        team_node = KnowledgeNode.objects.filter(
            node_level=3, ref_id=team.id, is_deleted=False
        ).first()
        if team_node:
            doc_count = count_docs_in_subtree(team_node.id)
            if doc_count > 0:
                return Response(
                    {"detail": f"该团队下有 {doc_count} 个文档，请先迁移或删除后再操作"},
                    status=400
                )

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
    - department: 部门经理 + 知识库管理员(kb_ops)
    - all: 部门经理 + 知识库管理员 + 超级管理员
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

        # 知识库管理员 + 超级管理员（部门及以上级别）
        if scope in ("department", "all"):
            kb_ops_users = User.objects.filter(
                roles__role__code__in=["kb_ops"], is_deleted=False, status="active"
            ).distinct()
            for k in kb_ops_users:
                _add(k, "知识库管理员")

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


class AccessApplicationView(APIView):
    """GET/POST /api/v1/auth/permissions/applications/
    GET: 当前用户的访问申请列表
    POST: 提交新的文档/团队/部门访问申请
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.users.models import AccessApplication
        apps = AccessApplication.objects.filter(applicant=request.user).order_by("-created_at")[:50]
        rows = []
        for a in apps:
            rows.append({
                "id": a.id,
                "target_type": a.target_type,
                "target_id": a.target_id,
                "action": a.action,
                "reason": a.reason,
                "status": a.status,
                "reviewer_comment": a.reviewer_comment,
                "created_at": a.created_at.isoformat() if a.created_at else "",
                "reviewed_at": a.reviewed_at.isoformat() if a.reviewed_at else "",
            })
        return Response({"rows": rows, "count": len(rows)})

    def post(self, request):
        from apps.users.models import AccessApplication
        target_type = (request.data.get("target_type") or "").strip()
        target_id = request.data.get("target_id")
        action = (request.data.get("action") or "read").strip()
        reason = (request.data.get("reason") or "").strip()

        if target_type not in ("doc", "team", "dept", "all"):
            return Response({"detail": "target_type 取值应为 doc/team/dept/all"}, status=400)
        if not target_id and target_type != "all":
            return Response({"detail": "target_id 必填"}, status=400)
        if action not in ("read", "download"):
            return Response({"detail": "action 取值应为 read/download"}, status=400)
        if not reason:
            return Response({"detail": "请填写申请理由"}, status=400)

        app = AccessApplication.objects.create(
            applicant=request.user,
            target_type=target_type,
            target_id=target_id,
            action=action,
            reason=reason,
            status="pending",
        )
        logger.info(f"AccessApplication created: id={app.id}, applicant={request.user.username}, "
                    f"target={target_type}:{target_id}, action={action}")
        return Response({
            "id": app.id,
            "detail": "申请已提交，等待审批",
            "status": "pending",
        }, status=201)


class AccessApplicationWithdrawView(APIView):
    """POST /api/v1/auth/permissions/applications/<id>/withdraw/
    撤回自己的访问申请（仅 pending 状态可撤回）
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        from apps.users.models import AccessApplication
        try:
            app = AccessApplication.objects.get(id=pk, applicant=request.user)
        except AccessApplication.DoesNotExist:
            return Response({"detail": "申请不存在"}, status=404)
        if app.status != "pending":
            return Response({"detail": f"当前状态 {app.status} 不可撤回"}, status=400)
        app.status = "withdrawn"
        app.save(update_fields=["status", "updated_at"])
        return Response({"detail": "已撤回", "status": "withdrawn"})
