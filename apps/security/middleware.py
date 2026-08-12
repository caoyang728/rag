"""
IP 风控中间件
- 白名单优先放行（白名单命中则直接放行，即使同 IP 也在黑名单中）
- 黑名单实时拦截（含过期时间）
- 支持三种匹配模式：单 IP / CIDR / 通配符（如 10.0.*.*）/ IP 范围（如 10.0.0.1-10.0.0.100）
- 未在名单中默认放行（可配置为严格模式）
"""
import ipaddress
import fnmatch
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

        from apps.security.models import IpWhitelist, IpBlacklist
        # 白名单优先放行：命中白名单直接放行，即使同 IP 也在黑名单中
        if _match_any_ip_or_cidr(ip, IpWhitelist.objects.filter(is_enabled=True)):
            return None
        # 黑名单：遍历所有活跃记录，支持通配符/范围匹配
        now = timezone.now()
        for black in IpBlacklist.objects.filter(is_active=True):
            if black.expires_at and black.expires_at < now:
                # 过期自动解封
                black.is_active = False
                black.save(update_fields=['is_active'])
                continue
            if _match_ip_pattern(ip, black.ip):
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


def _match_ip_pattern(ip: str, pattern: str) -> bool:
    """检查 IP 是否匹配指定模式（单 IP / CIDR / 通配符 / 范围）

    支持的模式格式：
    - 单 IP：10.0.0.1
    - CIDR：10.0.0.0/24
    - 通配符：10.0.*.* 或 10.*.*.*
    - IP 范围：10.0.0.1-10.0.0.100
    """
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return False

    # CIDR 格式
    if '/' in pattern:
        try:
            return ip_obj in ipaddress.ip_network(pattern, strict=False)
        except ValueError:
            return False

    # 通配符格式（如 10.0.*.*）
    if '*' in pattern:
        return _match_wildcard_ip(ip, pattern)

    # IP 范围格式（如 10.0.0.1-10.0.0.100）
    if '-' in pattern:
        try:
            start_str, end_str = pattern.split('-', 1)
            start = int(ipaddress.ip_address(start_str.strip()))
            end = int(ipaddress.ip_address(end_str.strip()))
            return start <= int(ip_obj) <= end
        except (ValueError, TypeError):
            return False

    # 单 IP 格式
    try:
        return ip_obj == ipaddress.ip_address(pattern)
    except ValueError:
        return False


def _match_wildcard_ip(ip: str, pattern: str) -> bool:
    """通配符匹配（仅支持 IPv4）：10.0.*.* 匹配 10.0.0.0~10.0.255.255"""
    ip_parts = ip.split('.')
    pat_parts = pattern.split('.')
    # 仅支持 IPv4 四段格式
    if len(ip_parts) != 4 or len(pat_parts) != 4:
        return False
    for ip_part, pat_part in zip(ip_parts, pat_parts):
        if pat_part == '*':
            continue
        if ip_part != pat_part:
            return False
    return True


def _match_any_ip_or_cidr(ip: str, qs) -> bool:
    """遍历查询集，检查 IP 是否匹配任一记录的 ip_or_cidr 模式"""
    for w in qs:
        if _match_ip_pattern(ip, w.ip_or_cidr):
            return True
    return False


def validate_ip_pattern(pattern: str) -> bool:
    """校验 IP 模式是否合法（单 IP / CIDR / 通配符 / 范围）

    用于后端创建白/黑名单时的格式校验。
    """
    if not pattern or not pattern.strip():
        return False
    pattern = pattern.strip()

    # CIDR 格式
    if '/' in pattern:
        try:
            ipaddress.ip_network(pattern, strict=False)
            return True
        except ValueError:
            return False

    # 通配符格式
    if '*' in pattern:
        parts = pattern.split('.')
        if len(parts) != 4:
            return False
        for part in parts:
            if part != '*' and not part.isdigit():
                return False
            if part.isdigit() and not (0 <= int(part) <= 255):
                return False
        return True

    # IP 范围格式
    if '-' in pattern:
        try:
            start_str, end_str = pattern.split('-', 1)
            start = ipaddress.ip_address(start_str.strip())
            end = ipaddress.ip_address(end_str.strip())
            # 起止必须是同类型（同为 IPv4 或 IPv6）
            if type(start) != type(end):
                return False
            return int(start) <= int(end)
        except (ValueError, TypeError):
            return False

    # 单 IP 格式
    try:
        ipaddress.ip_address(pattern)
        return True
    except ValueError:
        return False
