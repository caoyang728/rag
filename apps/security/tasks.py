"""
security 任务 - IP 黑名单清理 & 登录失败计数处理
"""
from loguru import logger
from datetime import timedelta

from celery import shared_task
from django.utils import timezone



@shared_task(name='security.expire_ip_blacklist', queue='default')
def expire_ip_blacklist():
    """定时清理过期 IP 封禁"""
    from apps.security.models import IpBlacklist
    now = timezone.now()
    n = IpBlacklist.objects.filter(is_active=True, expires_at__lt=now).update(is_active=False)
    logger.info('[Security] expired %d ip blacklist', n)
    return {'expired': n}


@shared_task(name='security.handle_login_fail', queue='default')
def handle_login_fail(ip: str, username: str = ''):
    """处理登录失败：连续失败 N 次自动封 M 分钟"""
    from django.conf import settings
    from apps.security.models import LoginAttempt, IpBlacklist

    window = timezone.now() - timedelta(minutes=15)
    fail_count = LoginAttempt.objects.filter(
        ip=ip, created_at__gte=window,
        result__in=['wrong_password', 'user_not_found', 'locked'],
    ).count()

    if fail_count >= settings.MAX_LOGIN_FAIL:
        expires = timezone.now() + timedelta(minutes=settings.BAN_DURATION_MIN)
        IpBlacklist.objects.update_or_create(
            ip=ip, is_active=True,
            defaults={
                'reason': 'login_fail',
                'detail': f'连续 {fail_count} 次登录失败',
                'fail_count': fail_count,
                'expires_at': expires,
            }
        )
        logger.warning('[Security] BANNED ip=%s until=%s', ip, expires)
        return {'banned': True, 'ip': ip, 'expires_at': expires.isoformat()}
    return {'banned': False, 'fail_count': fail_count}
