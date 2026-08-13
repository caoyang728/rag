"""认证相关视图：登录/登出/个人资料/修改密码/密码重置

业务逻辑（登录收尾、登录审计、密码策略、密码重置验证码）已下沉至 services/auth_service，
本文件仅保留参数校验与响应组装。
"""
from django.contrib.auth import get_user_model
from loguru import logger

from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.models import UserRoleRel, UserStatus
from apps.users.serializers import UserSerializer, ProfileUpdateSerializer
from apps.users.services.auth_service import (
    validate_password_policy, record_login_attempt, finalize_login,
    send_password_reset_code, verify_and_reset_password,
)
from apps.users.utils import _client_ip, _client_ua

User = get_user_model()


class LoginView(APIView):
    """POST /api/v1/auth/login/  -> {access, refresh, user}"""
    permission_classes = [AllowAny]
    throttle_scope = "login"

    def post(self, request):
        username = (request.data.get("username") or "").strip()
        password = request.data.get("password") or ""
        captcha_id = request.data.get("captcha_id") or ""
        captcha_code = request.data.get("captcha_code") or ""
        ip = _client_ip(request)
        ua = _client_ua(request)

        if not username or not password:
            return Response({"detail": "用户名或密码不能为空"}, status=400)

        # 验证码验证
        from apps.security.views import verify_captcha
        if not verify_captcha(captcha_id, captcha_code):
            record_login_attempt(username, None, ip, ua, "captcha_fail")
            return Response({"detail": "验证码错误"}, status=401)

        user = User.objects.filter(username=username, is_deleted=False).first()
        if not user or not user.check_password(password):
            record_login_attempt(
                username, user if user else None, ip, ua,
                "user_not_found" if not user else "wrong_password",
            )
            return Response({"detail": "用户名或密码错误"}, status=401)

        if user.status != UserStatus.ACTIVE:
            logger.warning(f"Login attempt for inactive user: {user.username}, status={user.status}")
            return Response({"detail": "用户名或密码错误"}, status=401)

        refresh = finalize_login(user, ip, ua)
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
                from rest_framework_simplejwt.tokens import RefreshToken
                RefreshToken(token).blacklist()
            except Exception:
                # 登出失败不应阻塞用户，但记录日志便于排查黑名单化异常
                logger.warning(f"LogoutView - failed to blacklist refresh token for user={request.user.username}")
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
        # 仅更新资料字段，避免全字段触发 last_login_ip（IPv4Address）类型报错
        update_fields = [f for f in ("real_name", "avatar_url", "phone") if f in data]
        if update_fields:
            update_fields.append('updated_at')
            u.save(update_fields=update_fields)
        return Response(UserSerializer(u).data)


class ResetPasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        old = request.data.get("old_password", "")
        new = request.data.get("new_password", "")
        # 密码强度策略校验（与重置密码共用一套规则）
        err = validate_password_policy(new, old)
        if err:
            return Response({"detail": err}, status=400)
        if not request.user.check_password(old):
            return Response({"detail": "旧密码错误"}, status=400)
        request.user.set_password(new)
        from django.utils import timezone
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
        from apps.security.views import verify_captcha

        email = (request.data.get("email") or "").strip().lower()
        captcha_id = request.data.get("captcha_id", "")
        captcha_code = request.data.get("captcha_code", "")

        if not email:
            return Response({"detail": "请输入邮箱"}, status=400)
        # 校验图形验证码，防止接口被刷
        if not verify_captcha(captcha_id, captcha_code):
            return Response({"detail": "图形验证码错误或已过期"}, status=400)

        ok, detail, status = send_password_reset_code(email)
        if not ok:
            return Response({"detail": detail}, status=status)
        return Response({"ok": True, "message": detail})


class PasswordResetConfirmView(APIView):
    """密码重置确认：验证邮箱 + 6 位验证码，通过后设置新密码。
    验证码从 Redis 读取，验证后立即删除（一次性使用）。
    """
    permission_classes = [AllowAny]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        code = request.data.get("code", "")
        new_password = request.data.get("new_password", "")

        # 参数校验
        if not email:
            return Response({"detail": "请输入邮箱"}, status=400)
        if not code:
            return Response({"detail": "请输入验证码"}, status=400)
        err = validate_password_policy(new_password)
        if err:
            return Response({"detail": err}, status=400)

        ok, detail, status = verify_and_reset_password(email, code, new_password)
        if not ok:
            return Response({"detail": detail}, status=status)
        return Response({"ok": True, "message": detail})
