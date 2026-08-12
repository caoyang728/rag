"""
apps.security 中间件单元测试 —— _match_ip_pattern / _match_wildcard_ip / validate_ip_pattern / process_request

与 test_views_middleware.py（集成测试）互补：这里直接实例化中间件与调用纯函数，
覆盖各模式的合法/非法/边界分支，不依赖真实请求链路。

覆盖范围：
- _match_ip_pattern：单 IP / CIDR / 通配符 / IP 范围的命中与不命中、非法模式容错
- _match_wildcard_ip：四段校验、通配匹配、段不匹配
- validate_ip_pattern：四种格式的合法/非法校验
- IpFilterMiddleware.process_request：无 IP 直接放行、黑名单 CIDR/通配符/范围匹配、异常模式不抛错
"""
import json
from unittest.mock import MagicMock

import pytest

from apps.security.middleware import (
    IpFilterMiddleware,
    _get_ip,
    _match_ip_pattern,
    _match_wildcard_ip,
    validate_ip_pattern,
)
from apps.security.models import IpBlacklist, IpWhitelist
from apps.security.tests.test_views import SecurityAPITestBase


class TestMatchIpPattern:
    """_match_ip_pattern 四种模式匹配逻辑"""

    def test_single_ip_match(self):
        assert _match_ip_pattern('10.0.0.5', '10.0.0.5') is True
        assert _match_ip_pattern('10.0.0.6', '10.0.0.5') is False

    def test_cidr_match(self):
        assert _match_ip_pattern('10.0.0.5', '10.0.0.0/24') is True
        assert _match_ip_pattern('10.1.0.5', '10.0.0.0/24') is False

    def test_wildcard_match(self):
        assert _match_ip_pattern('10.0.8.9', '10.0.*.*') is True
        assert _match_ip_pattern('10.9.8.9', '10.0.*.*') is False

    def test_range_match(self):
        assert _match_ip_pattern('10.0.0.50', '10.0.0.1-10.0.0.100') is True
        assert _match_ip_pattern('10.0.0.200', '10.0.0.1-10.0.0.100') is False

    def test_invalid_patterns_never_raise(self):
        # 非法 CIDR、非法范围、非法单 IP（无 / * - 字符）、非法 IP 均返回 False 且不抛异常
        assert _match_ip_pattern('10.0.0.5', '10.0.0.0/999') is False
        assert _match_ip_pattern('10.0.0.5', '10.0.0.1-xyz') is False
        assert _match_ip_pattern('10.0.0.5', '999.999.999.999') is False
        assert _match_ip_pattern('not-an-ip', '10.0.0.5') is False


class TestMatchWildcardIp:
    """通配符匹配（仅 IPv4 四段）"""

    def test_four_parts_match(self):
        assert _match_wildcard_ip('10.0.3.7', '10.0.*.*') is True

    def test_segment_mismatch(self):
        assert _match_wildcard_ip('10.1.3.7', '10.0.*.*') is False

    def test_non_four_parts_rejected(self):
        # IPv6 或不足四段均不匹配
        assert _match_wildcard_ip('::1', '10.0.*.*') is False
        assert _match_wildcard_ip('10.0.1', '10.0.*.*') is False


class TestValidateIpPattern:
    """validate_ip_pattern 四格式合法性校验"""

    def test_empty_rejected(self):
        assert validate_ip_pattern('') is False
        assert validate_ip_pattern('   ') is False
        assert validate_ip_pattern(None) is False

    def test_single_ip(self):
        assert validate_ip_pattern('10.0.0.5') is True
        assert validate_ip_pattern('300.0.0.5') is False

    def test_cidr(self):
        assert validate_ip_pattern('10.0.0.0/24') is True
        assert validate_ip_pattern('10.0.0.0/999') is False

    def test_wildcard(self):
        assert validate_ip_pattern('10.0.*.*') is True
        assert validate_ip_pattern('10.*.5.*') is True
        # 段数不对 / 段既非数字也非通配符 / 数字越界
        assert validate_ip_pattern('10.0.*') is False
        assert validate_ip_pattern('10.0.*.x') is False
        assert validate_ip_pattern('10.0.*.999') is False

    def test_range(self):
        assert validate_ip_pattern('10.0.0.1-10.0.0.100') is True
        # 起止顺序颠倒
        assert validate_ip_pattern('10.0.0.100-10.0.0.1') is False
        # IPv4 与 IPv6 混用
        assert validate_ip_pattern('10.0.0.1-::1') is False
        # 非法端点
        assert validate_ip_pattern('10.0.0.1-xyz') is False


class TestProcessRequestUnit:
    """process_request 直测：不依赖真实请求链路"""

    def _mw(self):
        return IpFilterMiddleware(get_response=lambda r: None)

    def _req(self, path='/api/v1/security/sensitive-words/', meta=None):
        req = MagicMock()
        req.path = path
        req.META = dict(meta or {})
        return req

    def test_no_ip_returns_none(self):
        """META 中无任何 IP 信息时直接放行（返回 None）"""
        assert self._mw().process_request(self._req(meta={})) is None

    @pytest.mark.django_db
    def test_blacklist_wildcard_blocks(self):
        """通配符黑名单命中返回 403"""
        IpBlacklist.objects.create(ip='10.0.*.*', reason='manual')
        resp = self._mw().process_request(self._req(meta={'REMOTE_ADDR': '10.0.7.7'}))
        assert resp.status_code == 403
        assert json.loads(resp.content)['code'] == 40301

    @pytest.mark.django_db
    def test_blacklist_range_blocks(self):
        """范围黑名单命中返回 403"""
        IpBlacklist.objects.create(ip='10.0.0.1-10.0.0.100', reason='manual')
        resp = self._mw().process_request(self._req(meta={'REMOTE_ADDR': '10.0.0.50'}))
        assert resp.status_code == 403

    @pytest.mark.django_db
    def test_blacklist_cidr_blocks(self):
        """CIDR 黑名单命中返回 403"""
        IpBlacklist.objects.create(ip='10.0.0.0/24', reason='manual')
        resp = self._mw().process_request(self._req(meta={'REMOTE_ADDR': '10.0.0.9'}))
        assert resp.status_code == 403

    @pytest.mark.django_db
    def test_blacklist_invalid_pattern_no_crash(self):
        """黑名单中存在非法模式时不抛异常、不拦截"""
        IpBlacklist.objects.create(ip='not-an-ip', reason='manual')
        assert self._mw().process_request(self._req(meta={'REMOTE_ADDR': '10.0.0.9'})) is None

    @pytest.mark.django_db
    def test_whitelist_priority_over_blacklist_range(self):
        """白名单命中优先放行（黑名单中同 IP 也不拦截）"""
        IpBlacklist.objects.create(ip='10.0.0.1-10.0.0.100', reason='manual')
        IpWhitelist.objects.create(ip_or_cidr='10.0.0.0/24')
        assert self._mw().process_request(self._req(meta={'REMOTE_ADDR': '10.0.0.50'})) is None


class TestGetIp:
    """_get_ip 边界"""

    def test_xff_whitespace_and_length(self):
        req = MagicMock()
        req.META = {'HTTP_X_FORWARDED_FOR': ' 1.2.3.4 , 5.6.7.8', 'REMOTE_ADDR': '9.9.9.9'}
        # XFF 取第一个并 strip；超过 64 字符截断
        assert _get_ip(req) == '1.2.3.4'
        long_ip = 'A' * 100
        req.META = {'HTTP_X_FORWARDED_FOR': long_ip}
        assert len(_get_ip(req)) == 64

    def test_remote_addr_fallback(self):
        req = MagicMock()
        req.META = {'REMOTE_ADDR': ' 9.9.9.9 '}
        assert _get_ip(req) == '9.9.9.9'
