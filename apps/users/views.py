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
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from pypinyin import pinyin, Style

from apps.users.models import (
    Department, Team, Role, Permission,
    RolePermissionRel, UserRoleRel, UserDeptScopeRel, UserTeamScopeRel, has_permission,
    get_user_permissions, get_user_managed_depts,
    get_user_managed_teams, get_user_data_scope_level, DataScope,
)
from apps.users.permissions import CanManageUsers
from apps.users.serializers import (
    UserSerializer, UserListSerializer, UserCreateSerializer, UserUpdateSerializer,
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
    writer.writerow(["用户名", "邮箱", "真实姓名", "部门", "团队", "状态", "最后登录", "创建时间"])
    for u in users_qs.select_related('department', 'team'):
        # 单团队 FK：user.team 指向唯一团队
        team_names = u.team.name if u.team and not u.team.is_deleted else ''
        writer.writerow([
            u.username, u.email, u.real_name,
            u.department.name if u.department else "",
            team_names,
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
        # 仅更新密码相关字段，避免 save() 全字段触发 last_login_ip（IPv4Address）类型报错
        request.user.save(update_fields=['password', 'password_changed_at'])
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
        user.save(update_fields=['password', 'password_changed_at'])
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
        """组长只能分配 contributor/viewer；部门经理可额外分配 team_leader

        viewer 为默认准入只读角色；contributor 为申请通过后获得的读/写/下载角色。
        """
        allowed_keys = ['contributor', 'viewer']
        if is_dept:
            allowed_keys = ['team_leader', 'contributor', 'viewer']
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
            user.save(update_fields=['password'])
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
    def _soft_delete(self, user):
        """软删除用户：仅标记 is_deleted，保留原 username/email 供恢复识别
        username 全局唯一阻止同名新建；email partial unique 允许同邮箱命中软删除记录 → 询问恢复
        """
        user.is_deleted = True
        user.deleted_at = timezone.now()
        user.status = "disabled"
        user.save()

    def destroy(self, request, *args, **kwargs):
        u = self.get_object()
        # 检查删除权限（所有规则都在这里判断）
        can_delete, msg = self._check_can_manage_user(u)
        if not can_delete:
            return Response({"detail": msg}, status=403)
        self._soft_delete(u)
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

        for target_user in targets:
            if target_user.id in valid_ids:
                self._soft_delete(target_user)
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

    # ---- 恢复已软删除用户 ----
    @action(detail=True, methods=["post"])
    def revive(self, request, pk=None):
        """恢复软删除用户：清除删除标记，清空原显式角色重新按 viewer 兜底
        业务背景：同邮箱视为同一人，恢复后保留原审计历史，但权限需重新申请
        """
        # 直接从全量数据中查询（包括已软删除），避免被默认 queryset 过滤掉
        u = User.objects.filter(pk=pk).first()
        if not u:
            return Response({"detail": "No User matches the given query."}, status=404)
        if not u.is_deleted:
            return Response({"detail": "该用户未处于删除状态"}, status=400)
        # 权限校验：复用删除权限，能删就能恢复
        can_revive, msg = self._check_can_manage_user(u)
        if not can_revive:
            return Response({"detail": msg}, status=403)
        # 恢复字段：可选覆盖 real_name/department_id/team_id/status
        real_name = request.data.get("real_name") or u.real_name
        department_id = request.data.get("department_id", u.department_id)
        team_ids = request.data.get("team_ids") or []
        status = request.data.get("status", "active")
        with transaction.atomic():
            u.is_deleted = False
            u.deleted_at = None
            u.status = status
            u.real_name = real_name
            u.department_id = department_id
            # 单团队 FK：取第一个 team_id
            u.team_id = team_ids[0] if team_ids else None
            u.save()
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
        CSV 列：用户名, 姓名, 邮箱, 部门, 团队, 状态
        导入用户默认 viewer 角色，不支持通过 CSV 指定角色
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

        # 预加载部门、团队映射（按名称查找，减少 N+1 查询）
        dept_map = {d.name: d for d in Department.objects.filter(is_deleted=False)}
        team_map = {(t.name, t.department_id): t for t in Team.objects.filter(is_deleted=False)}

        # 导入用户默认 viewer 角色
        viewer_role = Role.objects.filter(role_key='viewer').first()

        # 组长锁定部门/团队，与 create 逻辑一致
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

                # 解析状态
                status_val = "active"
                if status_str:
                    if status_str in ("启用", "active"):
                        status_val = "active"
                    elif status_str in ("禁用", "disabled"):
                        status_val = "disabled"
                    else:
                        reason = f"状态「{status_str}」无效，应为 启用/禁用"

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
                            user.save(update_fields=['password'])
                            # 导入用户默认 viewer 角色
                            if viewer_role:
                                UserRoleRel.objects.create(
                                    user=user, role_id=viewer_role.id, status='ACTIVE', granted_by=u
                                )
                            # 单团队 FK：直接设置 user.team_id
                            if team_id:
                                user.team_id = team_id
                                user.save(update_fields=['team', 'updated_at'])
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

    def get_queryset(self):
        # 支持 ?department_id=xxx 按部门过滤团队(用于申请权限时部门→团队级联选择)
        qs = super().get_queryset()
        dept_id = self.request.query_params.get("department_id")
        if dept_id:
            try:
                qs = qs.filter(department_id=int(dept_id))
            except (TypeError, ValueError):
                pass
        return qs

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
    返回当前用户拥有的权限(按模块分组)和角色列表。

    返回字段:
    - roles: 当前用户持有的活跃角色列表(含 scope 信息)
    - permission_groups: 按 module 分组的权限点(含 label,从 Permission 表查询)
    - is_super_admin: 是否超管(系统级快路径)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.users.models import Permission, ScopeType
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


def _serialize_chain_nodes(chain):
    """序列化审批链节点 —— 批量解析 approver_id → approver_name, 供前端展示"谁批准的"

    性能优化:收集所有 approver_id 后一次查询,避免 N+1。
    """
    from apps.users.models import User
    ids = {n.get('approver_id') for n in chain if n.get('approver_id')}
    user_map = {}
    if ids:
        user_map = {
            u.id: (u.real_name or u.username)
            for u in User.objects.filter(id__in=ids).only('id', 'real_name', 'username')
        }
    return [
        {
            'approver_role': n.get('approver_role'),
            'approver_id': n.get('approver_id'),
            'approver_name': user_map.get(n.get('approver_id'), ''),
            'status': n.get('status'),
            'comment': n.get('comment', ''),
            'approved_at': n.get('approved_at', ''),
        } for n in chain
    ]


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
    """
    permission_classes = [IsAuthenticated]

    # 团队级角色(同 scope 内互斥,高等级覆盖低等级)
    TEAM_ROLE_KEYS = ('viewer', 'contributor', 'team_leader')
    # 自助申请禁止的角色(只能由管理员发起工单)
    SELF_APPLY_FORBIDDEN_KEYS = ('super_admin',)

    def get(self, request):
        """返回当前用户发起的工单列表,字段对齐 PermissionApprovalTicket 真实结构"""
        from apps.users.models import (
            PermissionApprovalTicket, ScopeType,
            Department, Team, User,
        )
        tickets = PermissionApprovalTicket.objects.filter(
            applicant=request.user,
        ).select_related('target_user', 'role', 'previous_role').order_by('-created_at')[:100]

        rows = []
        for t in tickets:
            chain = t.approval_chain or []
            # 解析 scope 名称(部门/团队),便于前端直接展示
            scope_name = ''
            if t.scope_type == ScopeType.DEPT and t.scope_id:
                dept = Department.objects.filter(id=t.scope_id, is_deleted=False).only('name').first()
                scope_name = dept.name if dept else f'部门#{t.scope_id}'
            elif t.scope_type == ScopeType.TEAM and t.scope_id:
                team = Team.objects.filter(id=t.scope_id, is_deleted=False).only('name').first()
                scope_name = team.name if team else f'团队#{t.scope_id}'
            elif t.scope_type in (ScopeType.GLOBAL, ScopeType.NONE):
                scope_name = '全局'

            # 提取审批人姓名(最后一个有 approver_id 的节点)
            approver_name = ''
            reviewer_comment = ''
            if t.status in ('APPROVED', 'EXECUTED', 'REJECTED') and chain:
                for node in reversed(chain):
                    if node.get('approver_id'):
                        approver = User.objects.filter(id=node['approver_id']).first()
                        if approver:
                            approver_name = approver.real_name or approver.username
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
        from apps.users.models import (
            Role, ScopeType, TicketChangeType, UserRoleRel,
            UserTeamScopeRel, GrantStatus,
        )
        from apps.users.ticket_service import create_ticket

        role_key = (request.data.get('role_key') or '').strip()
        scope_type = (request.data.get('scope_type') or ScopeType.NONE).strip()
        scope_id = request.data.get('scope_id')
        change_type = (request.data.get('change_type') or TicketChangeType.GRANT).strip()
        previous_role_id = request.data.get('previous_role_id')
        reason = (request.data.get('reason') or '').strip()
        effective_from = request.data.get('effective_from')
        expires_at = request.data.get('expires_at')

        # ── 字段校验 ──
        if not role_key:
            return Response({'detail': 'role_key 必填(角色标识)'}, status=400)
        if scope_type not in (ScopeType.TEAM, ScopeType.DEPT, ScopeType.NONE, ScopeType.GLOBAL):
            return Response({'detail': 'scope_type 取值应为 TEAM/DEPT/NONE'}, status=400)
        if change_type not in (
            TicketChangeType.GRANT, TicketChangeType.REVOKE, TicketChangeType.ROLE_CHANGE,
        ):
            return Response({'detail': 'change_type 取值应为 GRANT/REVOKE/ROLE_CHANGE'}, status=400)
        if not reason:
            return Response({'detail': '请填写申请理由'}, status=400)

        # scope_id 必填校验:TEAM/DEPT 必须指定具体组织
        if scope_type in (ScopeType.TEAM, ScopeType.DEPT) and not scope_id:
            return Response({'detail': f'scope_type={scope_type} 时 scope_id 必填'}, status=400)

        # ── 角色校验 ──
        role = Role.objects.filter(role_key=role_key, is_deleted=False).first()
        if not role:
            return Response({'detail': f'角色不存在: {role_key}'}, status=400)

        # super_admin 不可自助申请(只能由现有超管发起双人复核工单)
        if role_key in self.SELF_APPLY_FORBIDDEN_KEYS:
            return Response({'detail': '超级管理员角色不可自助申请,请联系现有超管发起工单'}, status=403)

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

        # ── 调用工单服务创建审批工单 ──
        # create_ticket 内部会自动检测同团队角色互斥并转为 ROLE_CHANGE
        ip = _client_ip(request)
        ua = request.META.get('HTTP_USER_AGENT', '')[:256]
        try:
            ticket = create_ticket(
                applicant=request.user,
                target_user=request.user,  # 自助申请:被授权对象为申请人自身
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
        from apps.users.models import (
            PermissionApprovalTicket, TicketStatus,
            Department, Team, ScopeType,
        )
        from apps.users.ticket_service import _can_approve_for_role

        user = request.user
        # 页面访问入口权限校验：仅超级管理员/用户管理员/部门经理/团队组长/合规管理员可查询
        if not (user.is_super_admin
                or user.is_compliance_admin
                or has_permission(user, 'user.manage_all')
                or has_permission(user, 'user.manage')
                or has_permission(user, 'kb.manage_all')):
            # 非上述角色但作为某团队/部门 leader 的也允许（leader 绑定在 Team/Department.leader_id 上）
            is_leader = (
                Team.objects.filter(leader_id=user.id, is_deleted=False).exists()
                or Department.objects.filter(leader_id=user.id, is_deleted=False).exists()
            )
            if not is_leader:
                raise PermissionDenied("无审批权限")

        pending_tickets = PermissionApprovalTicket.objects.filter(
            status=TicketStatus.PENDING,
        ).select_related('applicant', 'target_user', 'role', 'previous_role')

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

            if not _can_approve_for_role(user, approver_role, ticket):
                continue

            # 解析 scope 名称（部门/团队）
            scope_name = ''
            if ticket.scope_type == ScopeType.DEPT and ticket.scope_id:
                dept = Department.objects.filter(id=ticket.scope_id, is_deleted=False).only('name').first()
                scope_name = dept.name if dept else f'部门#{ticket.scope_id}'
            elif ticket.scope_type == ScopeType.TEAM and ticket.scope_id:
                team = Team.objects.filter(id=ticket.scope_id, is_deleted=False).only('name').first()
                scope_name = team.name if team else f'团队#{ticket.scope_id}'
            elif ticket.scope_type == ScopeType.GLOBAL:
                scope_name = '全局'

            rows.append({
                'id': ticket.id,
                'ticket_no': ticket.ticket_no,
                'change_type': ticket.change_type,
                'applicant_id': ticket.applicant_id,
                'applicant_name': ticket.applicant.real_name or ticket.applicant.username,
                'applicant_email': ticket.applicant.email or '',
                'target_user_id': ticket.target_user_id,
                'target_user_name': ticket.target_user.real_name or ticket.target_user.username,
                'target_user_email': ticket.target_user.email or '',
                'role_id': ticket.role_id,
                'role_name': ticket.role.name if ticket.role else '',
                'role_key': ticket.role.role_key if ticket.role else '',
                'previous_role_id': ticket.previous_role_id,
                'previous_role_name': ticket.previous_role.name if ticket.previous_role else '',
                'previous_role_key': ticket.previous_role.role_key if ticket.previous_role else '',
                'scope_type': ticket.scope_type,
                'scope_id': ticket.scope_id,
                'scope_name': scope_name,
                'reason': ticket.reason,
                'effective_from': ticket.effective_from.isoformat() if ticket.effective_from else '',
                'expires_at': ticket.expires_at.isoformat() if ticket.expires_at else '',
                'approver_role': approver_role,
                'approver_id': node.get('approver_id'),
                'created_at': ticket.created_at.isoformat() if ticket.created_at else '',
                'current_step': ticket.current_step,
                'total_steps': len(chain),
                'approval_chain': _serialize_chain_nodes(chain),
            })

        return Response({
            'rows': rows,
            'count': len(rows),
        })


class TicketApproveView(APIView):
    """POST /api/v1/auth/permissions/tickets/<id>/approve/
    审批通过工单（共享审批池模式：任一匹配 approver_role 的用户均可审批）

    Body:
    - comment: string，审批意见（可选）
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        from apps.users.models import PermissionApprovalTicket, TicketStatus
        from apps.users.ticket_service import approve_ticket, _can_approve_for_role

        try:
            ticket = PermissionApprovalTicket.objects.get(pk=pk)
        except PermissionApprovalTicket.DoesNotExist:
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
        ua = request.META.get("HTTP_USER_AGENT", "")[:256]

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
        from apps.users.models import PermissionApprovalTicket, TicketStatus
        from apps.users.ticket_service import reject_ticket, _can_approve_for_role

        comment = (request.data.get("comment") or "").strip()
        if not comment:
            return Response({"detail": "驳回理由不能为空"}, status=400)

        try:
            ticket = PermissionApprovalTicket.objects.get(pk=pk)
        except PermissionApprovalTicket.DoesNotExist:
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
        ua = request.META.get("HTTP_USER_AGENT", "")[:256]

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
        ).select_related(
            'applicant', 'target_user', 'role', 'previous_role',
        ).order_by('-created_at')[:100]

        rows = []
        for t in tickets:
            chain = t.approval_chain or []
            data = _serialize_ticket_brief(t, chain)
            rows.append(data)

        return Response({
            'rows': rows,
            'count': len(rows),
        })


def _serialize_ticket_brief(ticket, chain=None, include_chain=True):
    """序列化工单概要 —— 供「我已审批 / 全部工单」等列表视图复用

    性能优化:caller 需预先 select_related('applicant','target_user','role','previous_role')。
    scope_name 解析会触发额外查询(仅 DEPT/TEAM 且有 scope_id 时),可接受(列表量小)。
    """
    from apps.users.models import Department, Team, ScopeType

    if chain is None:
        chain = ticket.approval_chain or []

    # 解析 scope 名称
    scope_name = ''
    if ticket.scope_type == ScopeType.DEPT and ticket.scope_id:
        dept = Department.objects.filter(id=ticket.scope_id, is_deleted=False).only('name').first()
        scope_name = dept.name if dept else f'部门#{ticket.scope_id}'
    elif ticket.scope_type == ScopeType.TEAM and ticket.scope_id:
        team = Team.objects.filter(id=ticket.scope_id, is_deleted=False).only('name').first()
        scope_name = team.name if team else f'团队#{ticket.scope_id}'
    elif ticket.scope_type == ScopeType.GLOBAL:
        scope_name = '全局'

    data = {
        'id': ticket.id,
        'ticket_no': ticket.ticket_no,
        'change_type': ticket.change_type,
        'status': ticket.status,
        'applicant_id': ticket.applicant_id,
        'applicant_name': ticket.applicant.real_name or ticket.applicant.username if ticket.applicant else '',
        'applicant_email': ticket.applicant.email if ticket.applicant else '',
        'target_user_id': ticket.target_user_id,
        'target_user_name': ticket.target_user.real_name or ticket.target_user.username if ticket.target_user else '',
        'target_user_email': ticket.target_user.email if ticket.target_user else '',
        'role_id': ticket.role_id,
        'role_name': ticket.role.name if ticket.role else '',
        'role_key': ticket.role.role_key if ticket.role else '',
        'previous_role_id': ticket.previous_role_id,
        'previous_role_name': ticket.previous_role.name if ticket.previous_role else '',
        'previous_role_key': ticket.previous_role.role_key if ticket.previous_role else '',
        'scope_type': ticket.scope_type,
        'scope_id': ticket.scope_id,
        'scope_name': scope_name,
        'reason': ticket.reason,
        'effective_from': ticket.effective_from.isoformat() if ticket.effective_from else '',
        'expires_at': ticket.expires_at.isoformat() if ticket.expires_at else '',
        'created_at': ticket.created_at.isoformat() if ticket.created_at else '',
        'approved_at': ticket.approved_at.isoformat() if ticket.approved_at else '',
        'executed_at': ticket.executed_at.isoformat() if ticket.executed_at else '',
        'current_step': ticket.current_step,
        'total_steps': len(chain),
    }
    if include_chain:
        data['approval_chain'] = _serialize_chain_nodes(chain)
    return data


class ProcessedTicketsView(APIView):
    """GET /api/v1/auth/permissions/processed-tickets/
    当前用户已处理过的工单列表(我已审批视角)

    筛选逻辑:遍历非 PENDING 工单,检查 approval_chain 中是否存在 approver_id = 当前用户 的节点。
    包含 APPROVED / EXECUTED / REJECTED / CANCELLED 状态(只要当前用户审过某个节点即纳入)。
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.users.models import PermissionApprovalTicket, TicketStatus

        user = request.user
        # 与待审批列表共享入口权限:仅管理角色 / 合规管理员 / leader 可见
        if not (user.is_super_admin
                or user.is_compliance_admin
                or has_permission(user, 'user.manage_all')
                or has_permission(user, 'user.manage')
                or has_permission(user, 'kb.manage_all')):
            is_leader = (
                Team.objects.filter(leader_id=user.id, is_deleted=False).exists()
                or Department.objects.filter(leader_id=user.id, is_deleted=False).exists()
            )
            if not is_leader:
                raise PermissionDenied("无审批权限")

        # 排除待审批(PENDING)工单,只看已处理过的
        tickets = PermissionApprovalTicket.objects.exclude(
            status=TicketStatus.PENDING,
        ).select_related(
            'applicant', 'target_user', 'role', 'previous_role',
        ).order_by('-created_at')[:200]

        rows = []
        for ticket in tickets:
            chain = ticket.approval_chain or []
            # 检查当前用户是否在审批链中处理过某个节点
            my_node = None
            for node in chain:
                if node.get('approver_id') == user.id:
                    my_node = node
                    break
            if not my_node:
                continue

            data = _serialize_ticket_brief(ticket, chain)
            # 追加当前用户在该工单中的审批信息
            data['my_approver_role'] = my_node.get('approver_role', '')
            data['my_comment'] = my_node.get('comment', '')
            data['my_approved_at'] = my_node.get('approved_at', '')
            data['my_node_status'] = my_node.get('status', '')
            rows.append(data)

        return Response({
            'rows': rows,
            'count': len(rows),
        })


class AllTicketsView(APIView):
    """GET /api/v1/auth/permissions/all-tickets/
    全部工单列表(全局视角,仅 super_admin / compliance_admin 可访问)

    查询参数:
    - status: 按状态筛选(PENDING/APPROVED/EXECUTED/REJECTED/CANCELLED),不传则返回全部
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.users.models import PermissionApprovalTicket, TicketStatus

        user = request.user
        # 仅超级管理员 / 合规管理员可查看全部工单(审计视角)
        if not (user.is_super_admin or user.is_compliance_admin):
            raise PermissionDenied("仅超级管理员/合规管理员可查看全部工单")

        qs = PermissionApprovalTicket.objects.select_related(
            'applicant', 'target_user', 'role', 'previous_role',
        ).order_by('-created_at')

        status_filter = request.query_params.get('status', '').strip().upper()
        if status_filter:
            qs = qs.filter(status=status_filter)

        qs = qs[:500]

        rows = []
        for ticket in qs:
            chain = ticket.approval_chain or []
            data = _serialize_ticket_brief(ticket, chain)
            rows.append(data)

        return Response({
            'rows': rows,
            'count': len(rows),
        })


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
    - scope_type: 可选,筛选指定范围的角色(TEAM/DEPT/NONE)
    """
    permission_classes = [IsAuthenticated]

    # 角色分类(前端按分类分组展示)
    ROLE_CATEGORY_MAP = {
        'viewer': {'category': 'team', 'category_label': '团队角色', 'rank': 1},
        'contributor': {'category': 'team', 'category_label': '团队角色', 'rank': 2},
        'team_leader': {'category': 'team', 'category_label': '团队角色', 'rank': 3},
        'dept_manager': {'category': 'dept', 'category_label': '部门角色', 'rank': 10},
        'kb_admin': {'category': 'global', 'category_label': '全局高权角色', 'rank': 20},
        'compliance_admin': {'category': 'global', 'category_label': '全局高权角色', 'rank': 21},
        'user_admin': {'category': 'global', 'category_label': '全局高权角色', 'rank': 22},
        'super_admin': {'category': 'global', 'category_label': '全局高权角色', 'rank': 99},
    }

    # 审批链概要(前端展示用,不包含具体审批人)
    APPROVAL_CHAIN_SUMMARY = {
        'viewer': {'steps': ['目标团队组长'], 'desc': '目标团队组长单审(缺失降级)'},
        'contributor': {'steps': ['本团队组长', '目标团队组长'], 'desc': '本团队组长 → 目标团队组长 双审(跨团队);本团队单审'},
        'team_leader': {'steps': ['本部门经理', '目标部门经理'], 'desc': '本部门经理 → 目标部门经理 双审(跨部门);本部门单审'},
        'dept_manager': {'steps': ['用户管理员', '超级管理员'], 'desc': '用户管理员 → 超管 双审'},
        'kb_admin': {'steps': ['用户管理员', '超级管理员'], 'desc': '用户管理员 → 超管 双审'},
        'compliance_admin': {'steps': ['用户管理员', '超级管理员'], 'desc': '用户管理员 → 超管 双审'},
        'user_admin': {'steps': ['超级管理员', '超级管理员'], 'desc': '双超管复核(排除申请人,双人独立)'},
        'super_admin': {'steps': ['超级管理员', '超级管理员'], 'desc': '双超管复核(排除申请人,双人独立)'},
    }

    def get(self, request):
        from apps.users.models import Role, ScopeType

        scope_filter = (request.query_params.get('scope_type') or '').strip()
        # super_admin 不可自助申请,从返回清单中排除
        roles = Role.objects.filter(is_deleted=False).exclude(role_key='super_admin').order_by('id')

        rows = []
        for r in roles:
            meta = self.ROLE_CATEGORY_MAP.get(r.role_key)
            if not meta:
                # 未在分类表中登记的角色(如自定义角色)不返回
                continue
            # scope_type 筛选
            if scope_filter:
                category_scope_map = {
                    'team': ScopeType.TEAM,
                    'dept': ScopeType.DEPT,
                    'global': ScopeType.NONE,
                }
                if category_scope_map.get(meta['category']) != scope_filter:
                    continue

            chain_info = self.APPROVAL_CHAIN_SUMMARY.get(r.role_key, {'steps': [], 'desc': ''})
            # 需要绑定 scope 的角色类型
            need_scope = r.role_type in ('TEAM_SCOPE', 'DEPT_SCOPE')
            scope_type_required = ScopeType.TEAM if r.role_key in (
                'viewer', 'contributor', 'team_leader',
            ) else (ScopeType.DEPT if r.role_key == 'dept_manager' else ScopeType.NONE)

            rows.append({
                'id': r.id,
                'role_key': r.role_key,
                'name': r.name,
                'description': r.description or '',
                'role_type': r.role_type,
                'data_scope': r.data_scope,
                'category': meta['category'],
                'category_label': meta['category_label'],
                'rank': meta['rank'],
                'need_scope': need_scope,
                'scope_type_required': scope_type_required,
                'approval_steps': chain_info['steps'],
                'approval_desc': chain_info['desc'],
                'is_builtin': r.is_builtin,
            })

        # 按 rank 排序(等级低的在前,便于前端按层级展示)
        rows.sort(key=lambda x: x['rank'])

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
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.users.models import Role, ScopeType, TicketChangeType, Department, Team
        from apps.users.ticket_service import build_approval_chain, ApproverRole

        role_key = (request.query_params.get('role_key') or '').strip()
        scope_type = (request.query_params.get('scope_type') or ScopeType.NONE).strip()
        scope_id = request.query_params.get('scope_id')
        change_type = (request.query_params.get('change_type') or TicketChangeType.GRANT).strip()

        if not role_key:
            return Response({'detail': 'role_key 必填'}, status=400)
        if scope_type not in (ScopeType.TEAM, ScopeType.DEPT, ScopeType.NONE, ScopeType.GLOBAL):
            return Response({'detail': 'scope_type 取值应为 TEAM/DEPT/NONE'}, status=400)
        if scope_type in (ScopeType.TEAM, ScopeType.DEPT) and not scope_id:
            return Response({'detail': f'scope_type={scope_type} 时 scope_id 必填'}, status=400)

        role = Role.objects.filter(role_key=role_key, is_deleted=False).first()
        if not role:
            return Response({'detail': f'角色不存在: {role_key}'}, status=400)

        # scope_id 类型转换
        try:
            scope_id_int = int(scope_id) if scope_id else None
        except (TypeError, ValueError):
            return Response({'detail': 'scope_id 应为整数'}, status=400)

        # 构造审批链(不创建工单,仅预览)
        try:
            chain = build_approval_chain(
                applicant=request.user,
                target_user=request.user,
                change_type=change_type,
                role=role,
                scope_type=scope_type,
                scope_id=scope_id_int,
            )
        except Exception as e:
            logger.exception(f'[ChainPreview] 构造审批链失败: {e}')
            return Response({'detail': '构造审批链失败,请检查参数'}, status=400)

        # 解析每个节点的 scope 名称 + 审批人角色标签
        approver_role_labels = {
            ApproverRole.TEAM_LEADER: '团队组长',
            ApproverRole.DEPT_LEADER: '部门经理',
            ApproverRole.USER_ADMIN: '用户管理员',
            ApproverRole.SUPER_ADMIN: '超级管理员',
        }
        nodes = []
        for idx, node in enumerate(chain):
            node_scope_name = ''
            node_scope_type = node.get('approver_scope_type', ScopeType.NONE)
            node_scope_id = node.get('approver_scope_id')
            if node_scope_type == ScopeType.DEPT and node_scope_id:
                dept = Department.objects.filter(id=node_scope_id, is_deleted=False).only('name').first()
                node_scope_name = dept.name if dept else f'部门#{node_scope_id}'
            elif node_scope_type == ScopeType.TEAM and node_scope_id:
                team = Team.objects.filter(id=node_scope_id, is_deleted=False).only('name').first()
                node_scope_name = team.name if team else f'团队#{node_scope_id}'

            nodes.append({
                'step': idx + 1,
                'approver_role': node.get('approver_role'),
                'approver_role_label': approver_role_labels.get(node.get('approver_role'), node.get('approver_role')),
                'approver_scope_type': node_scope_type,
                'approver_scope_id': node_scope_id,
                'approver_scope_name': node_scope_name,
                'status': node.get('status', 'PENDING'),
            })

        # 解析申请目标 scope 名称
        target_scope_name = ''
        if scope_type == ScopeType.DEPT and scope_id_int:
            dept = Department.objects.filter(id=scope_id_int, is_deleted=False).only('name').first()
            target_scope_name = dept.name if dept else f'部门#{scope_id_int}'
        elif scope_type == ScopeType.TEAM and scope_id_int:
            team = Team.objects.filter(id=scope_id_int, is_deleted=False).only('name').first()
            target_scope_name = team.name if team else f'团队#{scope_id_int}'
        elif scope_type in (ScopeType.GLOBAL, ScopeType.NONE):
            target_scope_name = '全局'

        return Response({
            'role_key': role_key,
            'role_name': role.name,
            'change_type': change_type,
            'scope_type': scope_type,
            'scope_id': scope_id_int,
            'scope_name': target_scope_name,
            'chain': nodes,
            'total_steps': len(nodes),
            'is_direct_execute': len(nodes) == 0,
        })
