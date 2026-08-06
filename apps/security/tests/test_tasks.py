"""
apps.security.tasks 测试 —— 安全定时任务

覆盖范围：
- expire_ip_blacklist：清理已过期封禁（全部过期/部分过期/无过期）
- handle_login_fail：已有激活黑名单时 update_or_create 更新分支

说明：handle_login_fail 的阈值封禁/未达阈值分支已在 test_views_middleware.py
TestLoginLockout 中覆盖，本文件仅补其"已存在激活黑名单时更新"分支与 expire 任务。
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.security.tasks import expire_ip_blacklist, handle_login_fail
from apps.security.models import IpBlacklist, LoginAttempt


@pytest.mark.django_db
@pytest.mark.integration
class TestExpireIpBlacklist:
    """IP 黑名单过期清理任务测试"""

    def test_no_expired_keeps_all(self):
        """无过期记录时 expired=0，全部保持激活"""
        IpBlacklist.objects.create(ip='1.1.1.1', reason='manual',
                                   expires_at=timezone.now() + timedelta(hours=1))
        result = expire_ip_blacklist()
        assert result == {'expired': 0}
        assert IpBlacklist.objects.filter(ip='1.1.1.1', is_active=True).exists()

    def test_expired_deactivated(self):
        """过期记录应置为 is_active=False，未过期的不受影响"""
        IpBlacklist.objects.create(ip='1.1.1.1', reason='login_fail',
                                   expires_at=timezone.now() - timedelta(minutes=1))
        IpBlacklist.objects.create(ip='2.2.2.2', reason='manual',
                                   expires_at=timezone.now() + timedelta(hours=1))
        result = expire_ip_blacklist()
        assert result == {'expired': 1}
        assert not IpBlacklist.objects.filter(ip='1.1.1.1', is_active=True).exists()
        assert IpBlacklist.objects.filter(ip='2.2.2.2', is_active=True).exists()

    def test_null_expires_never_expired(self):
        """expires_at 为空的永久封禁不应被清理"""
        IpBlacklist.objects.create(ip='3.3.3.3', reason='manual', expires_at=None)
        result = expire_ip_blacklist()
        assert result == {'expired': 0}
        assert IpBlacklist.objects.filter(ip='3.3.3.3', is_active=True).exists()


@pytest.mark.django_db
@pytest.mark.integration
class TestHandleLoginFailUpdateExisting:
    """handle_login_fail 更新已有激活黑名单分支测试"""

    def test_existing_active_blacklist_updated(self):
        """同 IP 已存在激活黑名单时 update_or_create 应更新失败计数与过期时间"""
        now = timezone.now()
        IpBlacklist.objects.create(ip='10.0.0.1', reason='login_fail',
                                   detail='旧记录', fail_count=3,
                                   expires_at=now - timedelta(minutes=1))
        for _ in range(5):
            LoginAttempt.objects.create(username='x', ip='10.0.0.1', result='wrong_password')

        result = handle_login_fail('10.0.0.1', username='x')

        assert result['banned'] is True
        bl = IpBlacklist.objects.get(ip='10.0.0.1')
        # 记录被更新（数量应为 1，非新建）
        assert IpBlacklist.objects.filter(ip='10.0.0.1').count() == 1
        assert bl.fail_count == 5
        assert bl.expires_at > now
