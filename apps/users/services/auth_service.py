"""认证与账号安全业务逻辑：登录收尾、登录审计、密码策略、密码重置"""
import re
import secrets

from django.conf import settings
from django.utils import timezone
from loguru import logger

from rest_framework_simplejwt.tokens import RefreshToken

from apps.users.models import User


def validate_password_policy(new_password, old_password=None):
    """校验密码强度策略，返回错误信息或 None
    - 长度 8~32，须含大写/小写/数字
    - old_password 非空时禁止与旧密码相同
    供「修改密码」与「重置密码」两处共用，避免规则散落。
    """
    if len(new_password) < 8:
        return "新密码至少 8 位"
    if len(new_password) > 32:
        return "新密码最多 32 位"
    if old_password is not None and new_password == old_password:
        return "新密码不能与旧密码相同"
    if not re.search(r'[A-Z]', new_password):
        return "新密码必须包含大写字母"
    if not re.search(r'[a-z]', new_password):
        return "新密码必须包含小写字母"
    if not re.search(r'\d', new_password):
        return "新密码必须包含数字"
    return None


def record_login_attempt(username, user, ip, user_agent, result):
    """记录登录尝试（成功/失败）到 SecurityIncident 表，供 IP 分析

    审计可丢、业务不可丢：写失败仅记日志，绝不阻断登录主流程。
    """
    try:
        from apps.security.models import LoginAttempt
        LoginAttempt.objects.create(
            username=username[:64],
            user=user,
            ip=ip,
            user_agent=user_agent,
            result=result,
        )
    except Exception:
        logger.exception("write LoginAttempt failed")


def finalize_login(user, ip, user_agent):
    """登录成功收尾：last_login 快照 + 成功日志 + 生成 JWT token"""
    refresh = RefreshToken.for_user(user)
    user.last_login_at = timezone.now()
    user.last_login_ip = ip
    # 仅更新登录快照字段，避免 save() 全字段触发 last_login_ip（IPv4Address）类型报错
    user.save(update_fields=["last_login_at", "last_login_ip"])
    record_login_attempt(user.username, user, ip, user_agent, "success")
    return refresh


def send_password_reset_code(email):
    """生成 6 位验证码写入 Redis（5 分钟过期）并发送邮件

    安全考虑：无论邮箱是否存在都返回相同成功提示，避免被用于探测注册邮箱。
    返回 (ok, detail, status_code)：
    - 防刷（1 分钟内重复请求）→ (False, ..., 429)
    - 邮件发送失败 → (False, ..., 500)
    """
    from apps.security.views import _get_redis
    # 延迟导入 send_mail：保持测试可 patch django.core.mail.send_mail，
    # 避免模块加载时提前绑定函数对象导致 mock 失效
    from django.core.mail import send_mail

    user = User.objects.filter(email__iexact=email, is_deleted=False).first()
    if user:
        # 生成 6 位数字验证码（使用加密安全随机数，防止可预测性攻击）
        code = f"{secrets.randbelow(1000000):06d}"
        r = _get_redis()
        redis_key = f"pwd_reset:{email}"
        # 防刷：1 分钟内不可重复请求（300 - 240 = 60s 内视为刚发过）
        if r and r.exists(redis_key):
            ttl = r.ttl(redis_key)
            if ttl and ttl > 240:
                return False, "请求过于频繁，请 1 分钟后再试", 429
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
            return False, "验证码发送失败，请联系管理员", 500
    else:
        logger.warning(f"PasswordResetRequest - email not found: {email}")
    return True, "验证码已发送至该邮箱", 200


def verify_and_reset_password(email, code, new_password):
    """校验邮箱 + 6 位验证码，通过后重置密码（验证码一次性使用）

    返回 (ok, detail, status_code)。
    防暴力破解：同一邮箱最多允许 5 次失败尝试，超限后需重新获取验证码。
    """
    from apps.security.views import _get_redis

    r = _get_redis()
    if not r:
        return False, "服务暂时不可用，请稍后重试", 500

    # 防暴力破解：失败计数与验证码同生命周期（5 分钟）
    fail_key = f"pwd_reset_fail:{email}"
    fail_count = int(r.get(fail_key) or 0)
    if fail_count >= 5:
        return False, "验证码尝试次数过多，请重新获取", 429

    redis_key = f"pwd_reset:{email}"
    stored_code = r.get(redis_key)
    if not stored_code:
        return False, "验证码已过期，请重新获取", 400
    # _get_redis 使用 decode_responses=True，返回的是 str
    if stored_code != code:
        # 递增失败计数，与验证码同 TTL
        r.incr(fail_key)
        r.expire(fail_key, 300)
        return False, "验证码错误", 400
    # 验证通过，立即删除验证码和失败计数（一次性使用）
    r.delete(redis_key)
    r.delete(fail_key)

    # 查找用户并重置密码
    user = User.objects.filter(email__iexact=email, is_deleted=False).first()
    if not user:
        return False, "账号不存在", 400
    user.set_password(new_password)
    user.password_changed_at = timezone.now()
    user.save(update_fields=['password', 'password_changed_at'])
    logger.info(f"PasswordResetConfirm - password reset for user={user.username}")
    return True, "密码已重置，请使用新密码登录", 200
