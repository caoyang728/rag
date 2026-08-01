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

from apps.users.models import (
    Department, Team, Role, Permission,
    RolePermissionRel, UserRoleRel, has_permission,
    get_user_permissions, get_user_managed_depts,
    get_user_managed_teams, get_user_data_scope_level, DataScope,
)
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
    for u in users_qs.select_related('department', 'team').prefetch_related('user_role_rels__role'):
        # 单团队 FK：user.team 指向唯一团队
        team_names = u.team.name if u.team and not u.team.is_deleted else ''
        role_names = ', '.join(r.role.name for r in u.user_role_rels.all() if r.role) or ''
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
        # UserRoleRel 通过 status='ACTIVE' 过滤有效授权
        # user.team 为单团队 FK，select_related 直接加载
        from django.db.models import Prefetch
        user_with_related = User.objects.filter(id=u.id)\
            .select_related('department', 'team')\
            .prefetch_related(
                Prefetch('user_role_rels', queryset=UserRoleRel.objects.select_related('role').filter(status='ACTIVE')),
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


class PasswordResetRequestView(APIView):
    """密码重置请求：接收邮箱 + 图形验证码，验证通过后生成 6 位验证码发送至邮箱。
    验证码存入 Redis（5 分钟过期），用户在前端输入验证码 + 新密码完成重置。
    安全考虑：无论邮箱是否存在，都返回相同的成功信息，避免被用于探测注册邮箱。
    """
    permission_classes = [AllowAny]

    def post(self, request):
        import random
        from django.conf import settings
        from django.core.mail import send_mail
        from apps.security.views import verify_captcha, _get_redis

        email = (request.data.get("email") or "").strip().lower()
        captcha_id = request.data.get("captcha_id", "")
        captcha_code = request.data.get("captcha_code", "")

        if not email:
            return Response({"detail": "请输入邮箱"}, status=400)
        # 校验图形验证码，防止接口被刷
        if not verify_captcha(captcha_id, captcha_code):
            return Response({"detail": "图形验证码错误或已过期"}, status=400)

        user = User.objects.filter(email__iexact=email, is_deleted=False).first()
        if user:
            # 生成 6 位数字验证码
            code = f"{random.randint(0, 999999):06d}"
            r = _get_redis()
            redis_key = f"pwd_reset:{email}"
            # 防刷：1 分钟内不可重复请求
            if r and r.exists(redis_key):
                ttl = r.ttl(redis_key)
                if ttl and ttl > 240:  # 距上次发送不到 1 分钟（300 - 240 = 60s）
                    return Response({"detail": "请求过于频繁，请 1 分钟后再试"}, status=429)
            # 存入 Redis，5 分钟过期
            if r:
                r.setex(redis_key, 300, code)
            # 发送验证码邮件
            try:
                send_mail(
                    subject="知库 Agent - 密码重置验证码",
                    message=f"您正在重置知库 Agent 账号密码。\n\n验证码：{code}\n\n该验证码 5 分钟内有效，如非本人操作，请忽略此邮件。",
                    from_email=settings.EMAIL_FROM,
                    recipient_list=[email],
                    fail_silently=False,
                )
                logger.info(f"PasswordResetRequest - code sent to {email}, user={user.username}")
            except Exception as e:
                logger.error(f"PasswordResetRequest - failed to send email to {email}: {e}")
                return Response({"detail": "验证码发送失败，请联系管理员"}, status=500)
        else:
            logger.warning(f"PasswordResetRequest - email not found: {email}")
        # 安全考虑：不暴露邮箱是否存在
        return Response({"ok": True, "message": "验证码已发送至该邮箱"})


class PasswordResetConfirmView(APIView):
    """密码重置确认：验证邮箱 + 6 位验证码，通过后设置新密码。
    验证码从 Redis 读取，验证后立即删除（一次性使用）。
    """
    permission_classes = [AllowAny]

    def post(self, request):
        from apps.security.views import _get_redis

        email = (request.data.get("email") or "").strip().lower()
        code = request.data.get("code", "")
        new_password = request.data.get("new_password", "")

        # 参数校验
        if not email:
            return Response({"detail": "请输入邮箱"}, status=400)
        if not code:
            return Response({"detail": "请输入验证码"}, status=400)
        if len(new_password) < 8:
            return Response({"detail": "新密码至少 8 位"}, status=400)
        if len(new_password) > 32:
            return Response({"detail": "新密码最多 32 位"}, status=400)
        if not re.search(r'[A-Z]', new_password):
            return Response({"detail": "新密码必须包含大写字母"}, status=400)
        if not re.search(r'[a-z]', new_password):
            return Response({"detail": "新密码必须包含小写字母"}, status=400)
        if not re.search(r'\d', new_password):
            return Response({"detail": "新密码必须包含数字"}, status=400)

        # 从 Redis 读取验证码
        r = _get_redis()
        if not r:
            return Response({"detail": "服务暂时不可用，请稍后重试"}, status=500)
        redis_key = f"pwd_reset:{email}"
        stored_code = r.get(redis_key)
        if not stored_code:
            return Response({"detail": "验证码已过期，请重新获取"}, status=400)
        # _get_redis 使用 decode_responses=True，返回的是 str
        if stored_code != code:
            return Response({"detail": "验证码错误"}, status=400)
        # 验证通过，立即删除验证码（一次性使用）
        r.delete(redis_key)

        # 查找用户并重置密码
        user = User.objects.filter(email__iexact=email, is_deleted=False).first()
        if not user:
            return Response({"detail": "账号不存在"}, status=400)
        user.set_password(new_password)
        user.password_changed_at = timezone.now()
        user.save()
        logger.info(f"PasswordResetConfirm - password reset for user={user.username}")
        return Response({"ok": True, "message": "密码已重置，请使用新密码登录"})


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.filter(is_deleted=False).order_by("-created_at")
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated, CanManageUsers]
    # 允许排序的字段（DRF OrderingFilter 自动识别 ordering 查询参数）
    ordering_fields = ['username', 'email', 'real_name', 'created_at', 'last_login_at']
    ordering = '-created_at'

    # ---------- 权限辅助 ----------
    def _check_user_manage(self, target_user=None):
        """检查请求者是否有用户管理权限（及其范围）

        基于 permission_key + data_scope 判定：
        - user.manage_all：全局用户管理
        - user.manage + DEPT scope：部门属地范围内用户管理
        - user.manage + TEAM scope：团队属地范围内用户管理
        """
        u = self.request.user
        # 超级管理员永远放行（系统级快路径）
        if u.is_super_admin:
            return True
        # 拥有 user.manage_all 权限可管理全局用户
        if has_permission(u, 'user.manage_all'):
            return True
        # 拥有 user.manage 权限：按数据范围等级判定
        if has_permission(u, 'user.manage'):
            if target_user:
                u_scope = get_user_data_scope_level(u)
                # 部门级：可管理本部门（含属地授权部门）用户
                if u_scope == DataScope.DEPT:
                    managed_depts = get_user_managed_depts(u)
                    if target_user.department_id and target_user.department_id in managed_depts:
                        return True
                # 团队级：可管理本团队（含属地授权团队）用户
                if u_scope == DataScope.TEAM:
                    managed_teams = get_user_managed_teams(u)
                    if target_user.team_id and target_user.team_id in managed_teams:
                        return True
            return False
        return False

    def _check_can_manage_user(self, target_user):
        """检查是否可以禁用/启用/删除用户（基于 permission_key 判定，避免角色硬编码）

        通过 permission_key + data_scope 判定：
        - super_admin 系统级快路径
        - user.manage_all（GLOBAL scope）：可禁用除超管和其他 manage_all 持有者外的用户
        - user.manage（DEPT scope）：只能禁用本部门非管理者
        - user.manage（TEAM scope）：只能禁用本团队非管理者
        """
        u = self.request.user
        # 规则1：必须有用户管理权限（super_admin / user.manage_all / user.manage）
        if not (u.is_super_admin or has_permission(u, 'user.manage_all') or has_permission(u, 'user.manage')):
            return False, "没有禁用权限"

        # 规则2：不能操作自己
        if u.id == target_user.id:
            return False, "不能禁用自己"

        # 规则3：超级管理员不能被禁用（系统级快路径保护，防止锁死管理入口）
        if target_user.is_super_admin:
            return False, "超级管理员不能被禁用"

        # 规则4：超级管理员可以禁用除超级管理员以外的所有用户
        if u.is_super_admin:
            return True, ""

        # 以下按数据范围等级判定（GLOBAL > DEPT > TEAM）
        u_scope = get_user_data_scope_level(u)

        # 规则5：全局级（user.manage_all）——可禁用除超管和其他 manage_all 持有者外的用户
        if u_scope == DataScope.GLOBAL:
            if has_permission(target_user, 'user.manage_all'):
                return False, "不能禁用同级用户管理员"
            return True, ""

        # 规则6：部门级管理者——只能操作本部门（含属地授权部门）用户
        if u_scope == DataScope.DEPT:
            managed_depts = get_user_managed_depts(u)
            if not target_user.department_id or target_user.department_id not in managed_depts:
                return False, "只能禁用本部门员工"
            # 不能禁用同级管理者（拥有 user.manage / user.manage_all 的用户）
            if has_permission(target_user, 'user.manage') or has_permission(target_user, 'user.manage_all'):
                return False, "不能禁用同级部门经理"
            return True, ""

        # 规则7：团队级管理者——只能操作本团队（含属地授权团队）用户
        if u_scope == DataScope.TEAM:
            managed_teams = get_user_managed_teams(u)
            if not target_user.team_id or target_user.team_id not in managed_teams:
                return False, "只能禁用本组员工"
            # 不能禁用同级管理者
            if has_permission(target_user, 'user.manage') or has_permission(target_user, 'user.manage_all'):
                return False, "不能禁用同级团队组长"
            return True, ""

        return False, "无权限操作"

    def _get_manageable_user_ids(self):
        """获取当前用户可管理的用户ID集合

        返回 None 表示可管理所有用户（super_admin / user.manage_all）。
        """
        u = self.request.user
        # 拥有 user.manage_all 权限可管理所有用户（RBAC）
        if u.is_super_admin or has_permission(u, 'user.manage_all'):
            return None
        # 拥有 user.manage 权限：按 data_scope 判定管理范围
        if has_permission(u, 'user.manage'):
            u_scope = get_user_data_scope_level(u)
            # 部门级：可管理本部门（含属地授权部门）用户
            if u_scope == DataScope.DEPT:
                managed_depts = get_user_managed_depts(u)
                return set(User.objects.filter(department_id__in=managed_depts, is_deleted=False).values_list('id', flat=True))
            # 团队级：可管理本团队（含属地授权团队）用户
            if u_scope == DataScope.TEAM:
                managed_teams = get_user_managed_teams(u)
                return set(User.objects.filter(team_id__in=managed_teams, is_deleted=False).values_list('id', flat=True))
        # 普通用户只能管理自己
        return {u.id}

    def _filter_downward_roles(self, role_ids, is_dept):
        """组长只能分配普通员工；部门经理只能分配组长/普通员工（不能分配其他部门经理）

        employee 为随人事归属生效的默认兜底角色；read_only_employee 为显式授权的只读角色。
        """
        allowed_keys = ['employee', 'read_only_employee']
        if is_dept:
            allowed_keys = ['team_leader', 'employee', 'read_only_employee']
        allowed_ids = set(Role.objects.filter(role_key__in=allowed_keys).values_list('id', flat=True))
        return [rid for rid in (role_ids or []) if rid in allowed_ids]

    def _filter_role_ids(self, role_ids):
        """检查角色ID，非超管不能分配高级角色，检测到受限角色时抛出403错误

        通过 permission_key 判定受限角色：拥有 kb.manage_all / user.manage_all
        权限的角色视为高级角色；super_admin 角色也受限（系统级快路径角色）。
        """
        u = self.request.user
        # 超级管理员可以分配任意角色
        if u.is_super_admin:
            return role_ids
        # 非超管不能分配高级角色：通过 RolePermissionRel 反查拥有 *_manage_all 权限的角色
        restricted_ids = set(RolePermissionRel.objects.filter(
            permission__permission_key__in=['kb.manage_all', 'user.manage_all'],
            is_active=True,
        ).values_list('role_id', flat=True))
        # super_admin 角色也受限（系统级快路径角色，不可委派）
        sa_role_id = Role.objects.filter(role_key='super_admin').values_list('id', flat=True).first()
        if sa_role_id:
            restricted_ids.add(sa_role_id)
        has_restricted = role_ids and restricted_ids & set(role_ids)
        if has_restricted:
            raise PermissionDenied("无权分配高级角色")
        return role_ids

    def _validate_role_uniqueness(self, user, role_ids, department_id=None, team_ids=None):
        """校验 dept_manager 和 team_leader 的唯一性约束"""
        if not role_ids:
            return None
        # 批量查询所有 role_id 对应的 role_key，避免 N+1
        role_map = dict(Role.objects.filter(id__in=role_ids).values_list('id', 'role_key'))
        dept_manager_role_id = None
        team_leader_role_id = None
        for rid, rkey in role_map.items():
            if rkey == 'dept_manager':
                dept_manager_role_id = rid
            if rkey == 'team_leader':
                team_leader_role_id = rid

        # 部门经理唯一性：同一部门只能有一个部门经理
        if dept_manager_role_id and department_id:
            existing = UserRoleRel.objects.filter(
                role__role_key='dept_manager',
                status='ACTIVE'
            ).exclude(user=user).filter(
                user__department_id=department_id,
                user__is_deleted=False
            ).first()
            if existing:
                return f"该部门已有部门经理：{existing.user.real_name or existing.user.username}"

        # 团队 leader 唯一性：同一团队只能有一个 team_leader
        # 单团队 FK：user__team_id 直接匹配
        if team_leader_role_id and team_ids:
            for tid in team_ids:
                existing = UserRoleRel.objects.filter(
                    role__role_key='team_leader',
                    status='ACTIVE'
                ).exclude(user=user).filter(
                    user__team_id=tid,
                    user__is_deleted=False
                ).first()
                if existing:
                    team_name = Team.objects.filter(id=tid).values_list('name', flat=True).first() or f'团队#{tid}'
                    return f"团队“{team_name}”已有团队组长：{existing.user.real_name or existing.user.username}"
        return None

    # ---------- 查询（限定范围）----------
    def get_queryset(self):
        qs = super().get_queryset()
        # 添加预加载，减少N+1查询（user_role_rels 为用户-角色关联；team 单 FK select_related）
        qs = qs.select_related('department', 'team')\
               .prefetch_related('user_role_rels__role')
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
                # user_role_rels 为用户-角色关联表
                qs = qs.filter(user_role_rels__role_id=int(role_id))
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
        # 超管或拥有 user.manage_all 权限直接放行
        is_super = u.is_super_admin
        can_manage_all = has_permission(u, 'user.manage_all')
        # user.manage 权限 + data_scope 区分部门级/团队级
        is_dept = (not is_super and not can_manage_all
                   and has_permission(u, 'user.manage')
                   and get_user_data_scope_level(u) == DataScope.DEPT)
        is_team = (not is_super and not can_manage_all and not is_dept
                   and has_permission(u, 'user.manage')
                   and get_user_data_scope_level(u) == DataScope.TEAM)
        if not is_super and not can_manage_all and not is_dept and not is_team:
            return Response({"detail": "无用户管理权限"}, status=403)
        ser = UserCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        role_ids = ser.validated_data.pop("role_ids", [])
        team_ids = ser.validated_data.pop("team_ids", [])
        department_id = ser.validated_data.pop("department_id", None)
        # 过滤角色ID，非超管不能分配高级角色；组长只能分配组长/普通员工
        role_ids = self._filter_role_ids(
            role_ids if (is_super or can_manage_all) else self._filter_downward_roles(role_ids, is_dept)
        )
        # 组长/部门经理自动锁定部门/团队
        if is_team:
            department_id = u.department_id
            if not team_ids:
                # 单团队 FK：组长默认分配到自己所属团队
                team_ids = [u.team_id] if u.team_id else []
        elif is_dept:
            department_id = department_id or u.department_id
        # 组长只能分配到自己可管理的团队
        if is_team and team_ids:
            my_teams = get_user_managed_teams(u)
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
                self._sync_role_leader(user, set(role_ids))
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
        # user.manage 权限 + data_scope 区分部门级/团队级
        has_team = (has_permission(u, 'user.manage')
                    and get_user_data_scope_level(u) == DataScope.TEAM)
        has_dept = (has_permission(u, 'user.manage')
                    and get_user_data_scope_level(u) == DataScope.DEPT)
        is_super = u.is_super_admin
        if not is_super:
            if not has_dept and not has_team:
                return Response({"detail": "无权限编辑该用户"}, status=403)
            # 部门经理/组长不能提权：过滤可分配角色
            if role_ids is not None:
                role_ids = self._filter_downward_roles(role_ids, is_dept=has_dept)
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
        # 过滤角色ID，非超管不能分配高级角色（防御层）
        if role_ids is not None:
            role_ids = self._filter_role_ids(role_ids)
        # 校验部门经理/团队leader唯一性 —— 必须在 ser.save() 之前，避免部分更新已写入
        if role_ids is not None or 'department_id' in request.data or team_ids is not None:
            check_role_ids = role_ids if role_ids is not None else list(
                UserRoleRel.objects.filter(user=instance, status='ACTIVE').values_list('role_id', flat=True)
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
                # 撤销不再保留的角色（status='REVOKED'）
                UserRoleRel.objects.filter(
                    user=user,
                    status='ACTIVE',
                ).exclude(role_id__in=role_ids).update(
                    status='REVOKED',
                    revoked_at=timezone.now(),
                    revoked_by=request.user
                )

                objs = [UserRoleRel(user=user, role_id=rid, status='ACTIVE', granted_by=request.user) for rid in role_ids]
                UserRoleRel.objects.bulk_create(
                    objs,
                    update_conflicts=True,
                    update_fields=['status', 'revoked_at', 'revoked_by'],
                    unique_fields=['user_id', 'role_id']
                )

                self._sync_role_leader(user, set(role_ids))
            if team_ids is not None:
                # 单团队 FK：取第一个 team_id（兼容前端传入数组）
                new_team_id = team_ids[0] if team_ids else None
                if new_team_id:
                    if not Team.objects.filter(id=new_team_id, is_deleted=False).exists():
                        return Response({"detail": "指定的团队不存在"}, status=400)
                user.team_id = new_team_id
                user.save(update_fields=['team', 'updated_at'])
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
        if not request.user.is_super_admin:
            return Response({"detail": "仅超级管理员可分配角色"}, status=403)
        u = self.get_object()
        role_ids = request.data.get("role_ids", [])
        with transaction.atomic():
            # 撤销不再保留的角色
            UserRoleRel.objects.filter(
                user=u,
                status='ACTIVE',
            ).exclude(role_id__in=role_ids).update(
                status='REVOKED',
                revoked_at=timezone.now(),
                revoked_by=request.user
            )

            if role_ids:
                objs = [UserRoleRel(
                    user=u,
                    role_id=rid,
                    granted_by=request.user,
                    status='ACTIVE',
                ) for rid in role_ids]
                UserRoleRel.objects.bulk_create(
                    objs,
                    update_conflicts=True,
                    update_fields=['status', 'revoked_at', 'revoked_by'],
                    unique_fields=['user_id', 'role_id']
                )

            self._sync_role_leader(u, set(role_ids))
        return Response({"ok": True})

    def _sync_role_leader(self, user, role_ids_set):
        """分配/移除 team_leader / dept_manager 角色时，同步更新 Team.leader_id / Department.leader_id"""
        role_key_map = dict(Role.objects.filter(
            role_key__in=['team_leader', 'dept_manager']
        ).values_list('id', 'role_key'))

        has_tl = any(role_key_map.get(rid) == 'team_leader' for rid in role_ids_set)
        has_dm = any(role_key_map.get(rid) == 'dept_manager' for rid in role_ids_set)

        if has_tl:
            # 单团队 FK：用户只有一个 team
            if user.team_id:
                Team.objects.filter(id=user.team_id, leader__isnull=True).update(leader=user)
        else:
            Team.objects.filter(leader=user).update(leader=None)

        if has_dm:
            if user.department_id:
                Department.objects.filter(id=user.department_id, leader__isnull=True).update(leader=user)
        else:
            Department.objects.filter(leader=user).update(leader=None)

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

    # ---- 批量导入 ----
    @action(detail=False, methods=["post"])
    def batch_import(self, request):
        """POST /api/v1/auth/users/batch_import/
        上传 CSV 文件批量导入员工，返回带「结果」和「原因」两列的 CSV 供下载。
        CSV 列：用户名, 姓名, 邮箱, 部门, 团队, 角色, 状态
        """
        u = request.user
        # 权限校验：与 create 一致，仅管理角色可导入
        is_super = u.is_super_admin
        can_manage_all = has_permission(u, 'user.manage_all')
        # user.manage 权限 + data_scope 区分部门级/团队级
        is_dept = (not is_super and not can_manage_all
                   and has_permission(u, 'user.manage')
                   and get_user_data_scope_level(u) == DataScope.DEPT)
        is_team = (not is_super and not can_manage_all and not is_dept
                   and has_permission(u, 'user.manage')
                   and get_user_data_scope_level(u) == DataScope.TEAM)
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

        # 预加载部门、团队、角色映射（按名称查找，减少 N+1 查询）
        dept_map = {d.name: d for d in Department.objects.filter(is_deleted=False)}
        team_map = {(t.name, t.department_id): t for t in Team.objects.filter(is_deleted=False)}
        role_map = {r.name: r for r in Role.objects.all()}

        # 非超管可分配的角色ID集合（与 _filter_downward_roles 一致）
        # employee 为兜底角色，read_only_employee 为显式授权只读角色
        if is_super or can_manage_all:
            allowed_role_ids = set(Role.objects.values_list('id', flat=True))
        elif is_dept:
            allowed_role_ids = set(Role.objects.filter(role_key__in=['team_leader', 'employee', 'read_only_employee']).values_list('id', flat=True))
        else:  # is_team
            allowed_role_ids = set(Role.objects.filter(role_key__in=['employee', 'read_only_employee']).values_list('id', flat=True))

        # 组长锁定部门/团队，与 create 逻辑一致
        # get_user_managed_teams（含属地授权团队）
        my_team_ids = list(get_user_managed_teams(u)) if is_team else []

        # 输出 CSV：原列 + 结果 + 原因
        out_buf = io.StringIO()
        out_buf.write('\ufeff')  # BOM for Excel
        writer = csv.writer(out_buf)
        writer.writerow(header + ["结果", "原因"])

        success_count = 0
        fail_count = 0
        for line_no, row in enumerate(rows[1:], start=2):
            def get_col(name):
                idx = col_map.get(name)
                if idx is None or idx >= len(row):
                    return ""
                return (row[idx] or "").strip()

            username = get_col("用户名")
            real_name = get_col("姓名")
            email = get_col("邮箱")
            dept_name = get_col("部门")
            team_name = get_col("团队")
            role_name = get_col("角色")
            status_str = get_col("状态")

            result = "失败"
            reason = ""

            # 行级校验
            if not username:
                reason = "用户名不能为空"
            elif not real_name:
                reason = "姓名不能为空"
            elif not email:
                reason = "邮箱不能为空"
            elif User.objects.filter(username=username, is_deleted=False).exists():
                reason = f"用户名「{username}」已存在"
            elif User.objects.filter(email=email, is_deleted=False).exists():
                reason = f"邮箱「{email}」已被使用"
            else:
                # 解析部门
                dept_id = None
                department = None
                if dept_name:
                    department = dept_map.get(dept_name)
                    if not department:
                        reason = f"部门「{dept_name}」不存在"
                    else:
                        dept_id = department.id

                # 非超管权限范围校验
                if not reason:
                    if is_team and dept_id and dept_id != u.department_id:
                        reason = "组长只能导入本部门员工"
                    elif is_dept and dept_id and dept_id != u.department_id:
                        reason = "部门经理只能导入本部门员工"

                # 解析团队
                team_id = None
                if not reason and team_name and dept_id:
                    team = team_map.get((team_name, dept_id))
                    if not team:
                        reason = f"团队「{team_name}」在部门「{dept_name}」下不存在"
                    else:
                        team_id = team.id
                        # 组长只能导入本团队
                        if is_team and team_id not in my_team_ids:
                            reason = "组长只能导入本团队员工"

                # 解析角色
                role_id = None
                if not reason and role_name:
                    role = role_map.get(role_name)
                    if not role:
                        reason = f"角色「{role_name}」不存在"
                    else:
                        role_id = role.id
                        # 非超管不能分配高级角色
                        if role_id not in allowed_role_ids:
                            reason = f"无权分配角色「{role_name}」"

                # 解析状态
                status_val = "active"
                if status_str:
                    if status_str in ("启用", "active"):
                        status_val = "active"
                    elif status_str in ("禁用", "disabled"):
                        status_val = "disabled"
                    else:
                        reason = f"状态「{status_str}」无效，应为 启用/禁用"

                # 校验部门经理/团队leader唯一性
                if not reason:
                    role_ids_list = [role_id] if role_id else []
                    conflict = self._validate_role_uniqueness(None, role_ids_list, dept_id, [team_id] if team_id else [])
                    if conflict:
                        reason = conflict

                # 创建用户
                if not reason:
                    try:
                        with transaction.atomic():
                            # 生成默认密码：与 create 一致
                            pwd = username[:1].upper() + username[1:].lower() + "@1234"
                            user = User.objects.create(
                                username=username,
                                email=email,
                                real_name=real_name,
                                department_id=dept_id,
                                status=status_val,
                            )
                            user.set_password(pwd)
                            user.save()
                            if role_id:
                                UserRoleRel.objects.create(
                                    user=user, role_id=role_id, status='ACTIVE', granted_by=u
                                )
                            # 单团队 FK：直接设置 user.team_id
                            if team_id:
                                user.team_id = team_id
                                user.save(update_fields=['team', 'updated_at'])
                            # 同步 Team.leader_id / Department.leader_id
                            if role_id:
                                self._sync_role_leader(user, {role_id})
                        result = "成功"
                        success_count += 1
                    except Exception as e:
                        reason = f"创建失败：{str(e)[:200]}"

            if result == "失败":
                fail_count += 1
            writer.writerow(row + [result, reason])

        # 返回带结果的 CSV 文件
        resp = HttpResponse(out_buf.getvalue(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="users_import_result.csv"'
        # 通过自定义 header 返回统计信息，前端可读取展示 toast
        resp["X-Import-Success"] = str(success_count)
        resp["X-Import-Fail"] = str(fail_count)
        logger.info(f"User.batch_import - user: {u.username}, success: {success_count}, fail: {fail_count}")
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
        writer.writerow(["用户名", "姓名", "邮箱", "部门", "团队", "角色", "状态"])
        writer.writerow(["zhangsan", "张三", "zhangsan@example.com", "研发部", "后端组", "普通员工", "启用"])
        writer.writerow(["lisi", "李四", "lisi@example.com", "研发部", "前端组", "只读员工", "启用"])
        writer.writerow(["wangwu", "王五", "王五@example.com", "", "", "文档管理员", "禁用"])
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
                # 部门经理：可见普通角色，可分配 team_leader/employee/read_only_employee
                roles = _role_values(Role.objects.exclude(role_key__in=['super_admin', 'user_admin']))
                assignable = _role_values(Role.objects.filter(role_key__in=['team_leader', 'employee', 'read_only_employee']))
            else:
                # 团队组长：可见 team_leader/employee/read_only_employee，仅可分配 employee/read_only_employee
                roles = _role_values(Role.objects.filter(role_key__in=['team_leader', 'employee', 'read_only_employee']))
                assignable = _role_values(Role.objects.filter(role_key__in=['employee', 'read_only_employee']))
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
            # 单团队 FK：直接 filter team_id
            qs = qs.filter(team_id=int(team_id))
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
            Prefetch('teams', queryset=Team.objects.filter(is_deleted=False).select_related('leader'))
        ).annotate(user_count=models.Count('users', filter=models.Q(users__is_deleted=False)))

    def _check_can_manage_dept(self):
        """检查是否有部门管理权限（RBAC：user.manage_all，通过 is_user_admin 属性判定）"""
        u = self.request.user
        if u.is_user_admin:
            return True
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("无部门管理权限")

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
            return Response({"detail": f"部门“{name}”已存在"}, status=400)

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
                return Response({"detail": f"部门“{name}”已存在"}, status=400)
            if leader_id is not None:
                self._set_leader(deleted_dept, leader_id)
            logger.info(f"Department.create - restored deleted department: {deleted_dept.name}")
            return Response(DepartmentSerializer(deleted_dept).data, status=201)

        ser = DepartmentWriteSerializer(data=data)
        ser.is_valid(raise_exception=True)
        try:
            dept = ser.save()
        except IntegrityError:
            return Response({"detail": f"部门“{name}”已存在"}, status=400)
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
    # Permission 使用 permission_key 三段式（module.resource.action）标识权限点
    queryset = Permission.objects.all().order_by("module", "permission_key")
    serializer_class = PermissionSerializer
    permission_classes = [IsAuthenticated]

    def _check_super_admin(self):
        if not self.request.user.is_super_admin:
            raise PermissionDenied("仅超级管理员可操作")

    def list(self, request, *args, **kwargs):
        self._check_super_admin()
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        self._check_super_admin()
        return super().retrieve(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        self._check_super_admin()
        logger.info(f"Permission.create - user: {request.user.username}, data: {request.data}")
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        self._check_super_admin()
        perm = self.get_object()
        # 内置权限点（is_builtin=True）核心字段不可修改
        if perm.is_builtin:
            forbidden = [k for k in request.data if k not in ('description', 'name')]
            if forbidden:
                logger.warning(f"Permission.update - user {request.user.username} tried to modify builtin fields of {perm.permission_key}")
                return Response({"detail": "内置系统权限不允许修改核心字段"}, status=403)
        logger.info(f"Permission.update - user: {request.user.username}, perm: {perm.permission_key}, data: {request.data}")
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        self._check_super_admin()
        perm = self.get_object()
        if perm.is_builtin:
            logger.warning(f"Permission.destroy - user {request.user.username} tried to delete builtin perm {perm.permission_key}")
            return Response({"detail": "内置系统权限不允许删除"}, status=403)
        ref_count = RolePermissionRel.objects.filter(permission=perm, is_active=True).count()
        if ref_count > 0:
            return Response({"detail": f"该权限点被 {ref_count} 个角色引用，请先解除角色关联"}, status=400)
        logger.info(f"Permission.destroy - user: {request.user.username}, perm: {perm.permission_key}")
        perm.delete()
        return Response(status=204)


class RoleViewSet(viewsets.ModelViewSet):
    queryset = Role.objects.all().order_by("id")
    serializer_class = RoleSerializer
    permission_classes = [IsAuthenticated]

    def _check_super_admin(self):
        if not self.request.user.is_super_admin:
            raise PermissionDenied("仅超级管理员可操作")

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action in ('list', 'retrieve'):
            qs = qs.prefetch_related('role_permissions')
        return qs

    def list(self, request, *args, **kwargs):
        self._check_super_admin()
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        self._check_super_admin()
        return super().retrieve(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        self._check_super_admin()
        logger.info(f"Role.create - user: {request.user.username}, data: {request.data}")
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        self._check_super_admin()
        partial = kwargs.get('partial', False)
        role = self.get_object()
        # 内置角色的 role_key 和 is_builtin 不可修改（API 字段名仍为 code，内部映射 role_key）
        if role.is_builtin:
            if 'code' in request.data and request.data['code'] != role.role_key:
                logger.warning(f"Role.update - user {request.user.username} tried to change code of builtin role {role.role_key}")
                return Response({"detail": "内置角色编码不可修改"}, status=400)
            if 'is_builtin' in request.data and request.data['is_builtin'] != role.is_builtin:
                logger.warning(f"Role.update - user {request.user.username} tried to change is_builtin of role {role.role_key}")
                return Response({"detail": "内置角色标记不可修改"}, status=400)
        if 'is_builtin' in request.data:
            logger.warning(f"Role.update - user {request.user.username} tried to set is_builtin on role {role.role_key}")
            return Response({"detail": "is_builtin 字段不可通过API修改"}, status=400)
        serializer = self.get_serializer(role, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        logger.info(f"Role.update - user: {request.user.username}, role: {role.role_key}, data: {request.data}")
        return Response(serializer.data)

    def destroy(self, request, *args, **kwargs):
        self._check_super_admin()
        role = self.get_object()
        if role.is_builtin:
            logger.warning(f"Role.destroy - user {request.user.username} tried to delete builtin role {role.role_key}")
            return Response({"detail": "内置角色不可删除"}, status=400)
        user_count = UserRoleRel.objects.filter(role=role, status='ACTIVE').count()
        if user_count > 0:
            return Response({"detail": f"该角色被 {user_count} 个用户使用，请先解除用户关联"}, status=400)
        logger.info(f"Role.destroy - user: {request.user.username}, role: {role.role_key}")
        role.delete()
        return Response(status=204)

    @action(detail=True, methods=["post"], url_path="assign-permissions")
    def assign_permissions(self, request, pk=None):
        """批量设置角色权限（全量覆盖）"""
        self._check_super_admin()
        role = self.get_object()
        perm_ids = request.data.get("permission_ids", [])
        # 参数校验：必须是列表，元素必须是正整数
        if not isinstance(perm_ids, list):
            return Response({"detail": "permission_ids 必须是数组"}, status=400)
        clean_ids = []
        seen = set()
        for pid in perm_ids:
            try:
                pid_int = int(pid)
                if pid_int <= 0:
                    return Response({"detail": f"无效的权限ID: {pid}"}, status=400)
                if pid_int not in seen:
                    seen.add(pid_int)
                    clean_ids.append(pid_int)
            except (TypeError, ValueError):
                return Response({"detail": f"无效的权限ID: {pid}"}, status=400)

        # 批量校验权限是否存在，只保留有效的
        existing_ids = set(Permission.objects.filter(id__in=clean_ids).values_list("id", flat=True))
        valid_ids = [pid for pid in clean_ids if pid in existing_ids]
        invalid_count = len(clean_ids) - len(valid_ids)

        with transaction.atomic():
            RolePermissionRel.objects.filter(
                role=role,
                is_active=True,
            ).exclude(permission_id__in=valid_ids).update(
                is_active=False,
                revoked_at=timezone.now(),
                revoked_by=request.user
            )

            if valid_ids:
                objs = [RolePermissionRel(
                    role=role,
                    permission_id=pid,
                    granted_by=request.user
                ) for pid in valid_ids]
                RolePermissionRel.objects.bulk_create(
                    objs,
                    update_conflicts=True,
                    update_fields=['is_active', 'revoked_at', 'revoked_by'],
                    unique_fields=['role_id', 'permission_id']
                )
        logger.info(f"Role.assign_permissions - user: {request.user.username}, role: {role.role_key}, count: {len(valid_ids)}")
        resp = {"detail": f"已更新 {len(valid_ids)} 个权限"}
        if invalid_count > 0:
            resp["skipped"] = invalid_count
        return Response(resp)


class TeamViewSet(viewsets.ModelViewSet):
    queryset = Team.objects.filter(is_deleted=False).order_by("id")\
        .select_related('leader', 'department')
    serializer_class = TeamSerializer
    permission_classes = [IsAuthenticated]

    def _check_can_manage_team(self, dept_id=None):
        """检查是否有团队管理权限（RBAC：user.manage_all 或 user.manage + DEPT scope）"""
        u = self.request.user
        # is_user_admin 判定 user.manage_all 权限（全局用户管理）
        if u.is_user_admin:
            return True
        # 部门经理只能管理自己部门的团队（user.manage + DEPT scope）
        if has_permission(u, 'user.manage') and get_user_data_scope_level(u) == DataScope.DEPT:
            if dept_id is None:
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied("部门经理仅可操作本部门团队")
            # get_user_managed_depts（含属地授权部门）
            manager_depts = get_user_managed_depts(u)
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
            return Response({"detail": f"部门“{dept.name}”下已存在团队“{name}”"}, status=400)

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
                return Response({"detail": f"部门“{dept.name}”下已存在团队“{name}”"}, status=400)
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
            return Response({"detail": f"部门“{dept.name}”下已存在团队“{name}”"}, status=400)
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
        # 单团队 FK：统计 User.team 指向该团队的用户
        user_count = User.objects.filter(team=team, is_deleted=False).count()
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
        u = request.user
        roles = [
            {
                "id": ur.role.id,
                "code": ur.role.role_key,
                "name": ur.role.name,
                "is_builtin": ur.role.is_builtin,
            }
            for ur in u.user_role_rels.select_related("role").filter(status='ACTIVE').all()
        ]
        # get_user_permissions（返回 permission_key 集合）
        perm_set = get_user_permissions(u)
        # 按模块分组，转换为前端友好的分组结构
        # 新权限点格式为三段式 module.resource.action（如 kb.document.read）
        groups = {}
        for key in perm_set:
            parts = key.split('.')
            module = parts[0] if parts else key
            action = parts[-1] if len(parts) > 1 else ""
            if module not in groups:
                groups[module] = []
            groups[module].append({
                "code": key,
                "action": action,
                "label": key,
            })
        return Response({
            "roles": roles,
            "permission_groups": groups,
            "is_super_admin": u.is_super_admin,
        })


class PermissionApproversView(APIView):
    """GET /api/v1/auth/permissions/approvers/?scope=team|department|all
    返回当前用户可选择的审批人列表。
    - team: 团队 leader + 部门经理
    - department: 部门经理 + 知识库管理员
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
                is_deleted=False, status="active",
                user_role_rels__status='ACTIVE',
                user_role_rels__role__role_permissions__is_active=True,
                user_role_rels__role__role_permissions__permission__permission_key='kb.manage_all',
            ).distinct()
            for k in kb_admin_users:
                _add(k, "知识库管理员")

        # 超级管理员（all 级别）——通过 role_key='super_admin' 反查
        if scope == "all":
            sa_users = User.objects.filter(
                is_deleted=False, status="active",
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
    GET: 当前用户的访问申请列表
    POST: 提交新的文档/团队/部门访问申请

    使用 PermissionApprovalTicket 实现访问申请工单。
    响应字段名保持不变（target_type/target_id/action 等）以兼容前端。
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.users.models import PermissionApprovalTicket
        apps = PermissionApprovalTicket.objects.filter(applicant=request.user).order_by("-created_at")[:50]
        rows = []
        for a in apps:
            # 从 approval_chain 提取审批意见（最后一个节点的 comment）
            reviewer_comment = ""
            if a.approval_chain:
                last_step = a.approval_chain[-1] if isinstance(a.approval_chain, list) else {}
                reviewer_comment = last_step.get("comment", "") if isinstance(last_step, dict) else ""
            rows.append({
                "id": a.id,
                "target_type": a.scope_type.lower() if a.scope_type else "",
                "target_id": a.scope_id,
                "action": a.change_type.lower() if a.change_type else "",
                "reason": a.reason,
                "status": a.status.lower() if a.status else "",
                "reviewer_comment": reviewer_comment,
                "created_at": a.created_at.isoformat() if a.created_at else "",
                "reviewed_at": a.approved_at.isoformat() if a.approved_at else "",
            })
        return Response({"rows": rows, "count": len(rows)})

    def post(self, request):
        import uuid
        from apps.users.models import (
            PermissionApprovalTicket, ScopeType, TicketChangeType, TicketStatus,
        )
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

        # 映射旧 target_type → 新 scope_type
        scope_type_map = {
            "doc": ScopeType.NONE, "team": ScopeType.TEAM,
            "dept": ScopeType.DEPT, "all": ScopeType.GLOBAL,
        }
        # 生成唯一工单号
        ticket_no = f"APP-{uuid.uuid4().hex[:12].upper()}"
        app = PermissionApprovalTicket.objects.create(
            ticket_no=ticket_no,
            applicant=request.user,
            target_user=request.user,  # 自助申请：被授权对象为申请人自身
            change_type=TicketChangeType.GRANT,
            scope_type=scope_type_map.get(target_type, ScopeType.NONE),
            scope_id=target_id,
            reason=reason,
            status=TicketStatus.PENDING,
        )
        logger.info(f"PermissionApprovalTicket created: id={app.id}, applicant={request.user.username}, "
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
        from apps.users.models import PermissionApprovalTicket, TicketStatus
        try:
            app = PermissionApprovalTicket.objects.get(id=pk, applicant=request.user)
        except PermissionApprovalTicket.DoesNotExist:
            return Response({"detail": "申请不存在"}, status=404)
        if app.status != TicketStatus.PENDING:
            return Response({"detail": f"当前状态 {app.status} 不可撤回"}, status=400)
        # CANCELLED（工单状态机）
        app.status = TicketStatus.CANCELLED
        app.save(update_fields=["status", "updated_at"])
        return Response({"detail": "已撤回", "status": "cancelled"})


class PendingApprovalTicketsView(APIView):
    """GET /api/v1/auth/permissions/pending-approvals/
    当前用户待审批的工单列表（共享审批池）

    筛选逻辑：
    - 工单 status=PENDING
    - 当前审批节点的 approver_role 匹配当前用户的角色/身份
    - 排除申请人自己的工单
    - 排除已被其他管理员处理的节点（approver_id 已回填且非当前用户）

    多人可见：多个管理员同时可见同一待审批工单
    先到先得：一人审批后，工单从其他人的待办列表消失
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.users.models import PermissionApprovalTicket, TicketStatus
        from apps.users.ticket_service import _can_approve_for_role

        user = request.user
        # 全量 PENDING 工单，在应用层过滤角色匹配（JSONField 不支持 ORM 查询 approver_role）
        pending_tickets = PermissionApprovalTicket.objects.filter(
            status=TicketStatus.PENDING,
        ).select_related('applicant', 'target_user')

        rows = []
        for ticket in pending_tickets:
            chain = ticket.approval_chain or []
            if ticket.current_step >= len(chain):
                continue
            node = chain[ticket.current_step]
            approver_role = node['approver_role']

            # 如果节点已被其他管理员处理（approver_id 已回填且非当前用户），跳过
            if node.get('approver_id') and node['approver_id'] != user.id:
                continue

            # 角色匹配校验
            if _can_approve_for_role(user, approver_role, ticket):
                rows.append({
                    'id': ticket.id,
                    'ticket_no': ticket.ticket_no,
                    'change_type': ticket.change_type,
                    'applicant_name': ticket.applicant.real_name or ticket.applicant.username,
                    'target_user_name': ticket.target_user.real_name or ticket.target_user.username,
                    'role_name': ticket.role.name if ticket.role else '',
                    'scope_type': ticket.scope_type,
                    'scope_id': ticket.scope_id,
                    'reason': ticket.reason,
                    'approver_role': approver_role,
                    'approver_id': node.get('approver_id'),
                    'created_at': ticket.created_at.isoformat() if ticket.created_at else '',
                    'current_step': ticket.current_step,
                    'total_steps': len(chain),
                })

        return Response({
            'rows': rows,
            'count': len(rows),
        })


class MyTicketsView(APIView):
    """GET /api/v1/auth/permissions/my-tickets/
    当前用户发起的工单列表（含待审批、已通过、已驳回、已执行等所有状态）

    用于查看自己申请的权限变更进度和历史。
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.users.models import PermissionApprovalTicket

        tickets = PermissionApprovalTicket.objects.filter(
            applicant=request.user,
        ).select_related('target_user', 'role').order_by('-created_at')[:100]

        rows = []
        for t in tickets:
            chain = t.approval_chain or []
            approver_name = ''
            if t.status in ('APPROVED', 'EXECUTED', 'REJECTED') and chain:
                # 取最后一个审批节点的 approver_id 对应的用户名
                for node in reversed(chain):
                    if node.get('approver_id'):
                        from apps.users.models import User
                        approver = User.objects.filter(id=node['approver_id']).first()
                        if approver:
                            approver_name = approver.real_name or approver.username
                        break

            rows.append({
                'id': t.id,
                'ticket_no': t.ticket_no,
                'change_type': t.change_type,
                'status': t.status,
                'role_name': t.role.name if t.role else '',
                'scope_type': t.scope_type,
                'scope_id': t.scope_id,
                'reason': t.reason,
                'approver_name': approver_name,
                'created_at': t.created_at.isoformat() if t.created_at else '',
                'approved_at': t.approved_at.isoformat() if t.approved_at else '',
                'executed_at': t.executed_at.isoformat() if t.executed_at else '',
                'current_step': t.current_step,
                'total_steps': len(chain),
            })

        return Response({
            'rows': rows,
            'count': len(rows),
        })
