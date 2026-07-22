"""
IP 风控中间件
- 白名单优先放行
- 黑名单实时拦截（含过期时间）
- 未在名单中默认放行（可配置为严格模式）
"""
import ipaddress
from loguru import logger
from typing import Optional

from django.http import JsonResponse
from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin



class IpFilterMiddleware(MiddlewareMixin):
    def process_request(self, request):
        # 跳过 admin 路径与静态资源
        path = request.path
        if not path.startswith('/api/'):
            return None

        ip = _get_ip(request)
        if not ip:
            return None

        # 白名单
        from apps.security.models import IpWhitelist, IpBlacklist
        # 命中白名单直接放行
        if _match_any_ip_or_cidr(ip, IpWhitelist.objects.filter(is_enabled=True)):
            return None
        # 黑名单
        now = timezone.now()
        black = IpBlacklist.objects.filter(ip=ip, is_active=True).first()
        if black:
            if black.expires_at and black.expires_at < now:
                # 过期自动解封
                black.is_active = False
                black.save(update_fields=['is_active'])
                return None
            return JsonResponse({
                'code': 40301, 'message': 'IP 已被封禁', 'ip': ip,
                'expires_at': black.expires_at.isoformat() if black.expires_at else None,
            }, status=403)
        return None


def _get_ip(request):
    ip = request.META.get('HTTP_X_FORWARDED_FOR', '') or request.META.get('REMOTE_ADDR', '')
    if ',' in ip:
        ip = ip.split(',')[0]
    return ip.strip()[:64]


def _match_any_ip_or_cidr(ip: str, qs) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for w in qs:
        pat = w.ip_or_cidr
        try:
            if '/' in pat:
                if ip_obj in ipaddress.ip_network(pat, strict=False):
                    return True
            elif ipaddress.ip_address(pat) == ip_obj:
                return True
        except Exception:
            continue
    return False
