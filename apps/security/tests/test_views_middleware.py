"""
apps.security 中间件与登录锁定测试 —— IpFilterMiddleware / handle_login_fail

覆盖范围：
- IP 中间件集成：白名单放行 / 黑名单 403 / 过期黑名单自动解封 / 非 API 路径跳过
- _match_any_ip_or_cidr 与 _get_ip 匹配逻辑
- 登录锁定行为：连续失败达到阈值由 handle_login_fail 自动封禁，封禁后请求被 403

测试分层：IP 中间件与登录锁定走真实 DB + 真实中间件链路验证端到端契约。
"""
import json
from unittest.mock import MagicMock

import pytest

from apps.security.models import IpWhitelist, IpBlacklist, LoginAttempt
from apps.security.middleware import _match_any_ip_or_cidr, _get_ip
from apps.security.tasks import handle_login_fail
from apps.security.tests.test_views import SecurityAPITestBase


class TestIpFilterMiddleware(SecurityAPITestBase):
    """IP 白/黑名单中间件端到端行为"""

    def _get(self, path, ip='127.0.0.1', headers=None):
        headers = dict(headers or {})
        headers.setdefault('REMOTE_ADDR', ip)
        return self.client.get(path, **headers)

    @pytest.mark.integration
    def test_blacklist_blocks_api(self):
        """命中黑名单的 IP 访问 API 返回 40301"""
        IpBlacklist.objects.create(ip='127.0.0.1', reason='manual')
        resp = self._get('/api/v1/security/sensitive-words/', ip='127.0.0.1',
                         headers=self.admin_headers)
        assert resp.status_code == 403
        assert resp.json()['code'] == 40301

    @pytest.mark.integration
    def test_blacklist_expired_auto_unblock(self):
        """过期黑名单自动解封并放行，is_active 置为 False"""
        from django.utils import timezone
        from datetime import timedelta
        IpBlacklist.objects.create(
            ip='127.0.0.1', reason='login_fail', is_active=True,
            expires_at=timezone.now() - timedelta(minutes=1))
        resp = self._get('/api/v1/security/sensitive-words/', ip='127.0.0.1',
                         headers=self.admin_headers)
        assert resp.status_code == 200
        bl = IpBlacklist.objects.get(ip='127.0.0.1')
        assert bl.is_active is False

    @pytest.mark.integration
    def test_whitelist_bypasses_blacklist(self):
        """白名单命中优先放行，即使同 IP 也在黑名单中"""
        IpBlacklist.objects.create(ip='10.0.0.9', reason='manual')
        IpWhitelist.objects.create(ip_or_cidr='10.0.0.0/24', created_by=self.super_admin)
        resp = self._get('/api/v1/security/sensitive-words/', ip='10.0.0.9',
                         headers=self.admin_headers)
        assert resp.status_code == 200

    @pytest.mark.integration
    def test_non_api_path_skipped(self):
        """非 /api/ 路径不经过 IP 风控"""
        IpBlacklist.objects.create(ip='127.0.0.1', reason='manual')
        resp = self._get('/admin/login/', ip='127.0.0.1')
        assert resp.status_code != 403

    @pytest.mark.integration
    def test_match_any_ip_or_cidr(self):
        """CIDR 与单 IP 匹配逻辑"""
        whitelist = [MagicMock(ip_or_cidr='10.0.0.0/24'), MagicMock(ip_or_cidr='192.168.1.5')]
        assert _match_any_ip_or_cidr('10.0.0.8', whitelist) is True
        assert _match_any_ip_or_cidr('192.168.1.5', whitelist) is True
        assert _match_any_ip_or_cidr('192.168.1.6', whitelist) is False
        # 非法 IP 与非法 CIDR 模式不匹配、不抛异常
        assert _match_any_ip_or_cidr('not-an-ip', whitelist) is False

    @pytest.mark.integration
    def test_get_ip_xff_and_remote_addr(self):
        """X-Forwarded-For 取第一个，无 XFF 时取 REMOTE_ADDR"""
        req = MagicMock()
        req.META = {'HTTP_X_FORWARDED_FOR': '1.2.3.4, 5.6.7.8', 'REMOTE_ADDR': '9.9.9.9'}
        assert _get_ip(req) == '1.2.3.4'
        req.META = {'REMOTE_ADDR': '9.9.9.9'}
        assert _get_ip(req) == '9.9.9.9'
        # 无 IP 时中间件直接放行
        req.META = {}
        assert _get_ip(req) == ''


class TestLoginLockout(SecurityAPITestBase):
    """登录失败阈值触发 IP 封禁，封禁后请求被中间件拦截"""

    @pytest.mark.integration
    def test_login_fail_ban_after_threshold(self):
        """15 分钟内连续失败达到 MAX_LOGIN_FAIL(5) 自动加入黑名单"""
        for _ in range(5):
            LoginAttempt.objects.create(
                username='alice', ip='10.9.9.9', result='wrong_password')
        result = handle_login_fail('10.9.9.9', username='alice')
        assert result['banned'] is True
        bl = IpBlacklist.objects.get(ip='10.9.9.9')
        assert bl.reason == 'login_fail'
        assert bl.fail_count == 5
        assert bl.expires_at is not None
        assert bl.is_active is True

    @pytest.mark.integration
    def test_login_fail_below_threshold_not_banned(self):
        """失败次数未达阈值时不封禁"""
        LoginAttempt.objects.create(username='bob', ip='10.9.9.10', result='wrong_password')
        result = handle_login_fail('10.9.9.10', username='bob')
        assert result['banned'] is False
        assert not IpBlacklist.objects.filter(ip='10.9.9.10').exists()

    @pytest.mark.integration
    def test_banned_ip_blocked_via_middleware(self):
        """封禁后的 IP 通过中间件拦截，登录接口返回 403"""
        IpBlacklist.objects.create(ip='127.0.0.1', reason='login_fail', detail='连续失败')
        resp = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'username': 'x', 'password': 'y'}),
            content_type='application/json')
        assert resp.status_code == 403
        assert resp.json()['code'] == 40301
