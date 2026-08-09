"""
apps.users.views 认证与账号补充测试 —— 登出 / 登录边界 / 资料 / 密码重置

与 test_views.py 互补：
- LogoutView + LoginView 边界（禁用用户、用户不存在）
- ProfileView 补充分支
- ResetPasswordView / PasswordResetRequestView / PasswordResetConfirmView
  （mock 验证码 + Redis + 邮件）
"""
import csv
import io
import json
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken

from apps.users.models import (
    User, Role, Department, Team, Permission, RolePermissionRel,
    UserRoleRel, UserDeptScopeRel, UserTeamScopeRel,
    TicketList, TicketStatus, TicketChangeType, ScopeType,
    GrantStatus, RoleType, DataScope,
)
from apps.users.tests.test_views_base import (
    _get_or_create_role, _create_user, _grant_permission, _grant_global_role,
    _auth_headers, FakeRedis, UsersAPIExtraBase,
)


class TestLogoutAndLoginEdge(UsersAPIExtraBase):
    """LogoutView + LoginView 边界场景"""

    @pytest.mark.integration
    def test_logout_authenticated_200(self):
        """已登录用户携带 refresh token 登出应返回 200（token_blacklist 未启用时静默通过）"""
        refresh = RefreshToken.for_user(self.normal_user)
        resp = self.client.post(
            '/api/v1/auth/logout/',
            data=json.dumps({'refresh': str(refresh)}),
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['ok'] is True

    @pytest.mark.integration
    def test_logout_without_token_200(self):
        """登出时不传 refresh token 也应返回 200（幂等）"""
        resp = self.client.post(
            '/api/v1/auth/logout/',
            data=json.dumps({}),
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 200

    @pytest.mark.integration
    def test_logout_anonymous_401(self):
        """匿名登出应 401（IsAuthenticated 拦截）"""
        resp = self.client.post('/api/v1/auth/logout/')
        assert resp.status_code in (401, 403)

    @patch('apps.security.views.verify_captcha', return_value=True)
    @pytest.mark.integration
    def test_login_disabled_user_401(self, _mock_captcha):
        """已禁用用户登录应 401（与密码错误返回一致，不暴露账号状态）"""
        disabled = _create_user(username='disabled_user', status='disabled')
        resp = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'username': 'disabled_user', 'password': 'pass12345'}),
            content_type='application/json',
        )
        assert resp.status_code == 401
        assert disabled.status == 'disabled'

    @patch('apps.security.views.verify_captcha', return_value=True)
    @pytest.mark.integration
    def test_login_user_not_found_401(self, _mock_captcha):
        """不存在的用户名登录应 401"""
        resp = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'username': 'ghost_user', 'password': 'pass12345'}),
            content_type='application/json',
        )
        assert resp.status_code == 401


# ============================================================================
# ProfileView 补充 —— 头像/手机号更新
# ============================================================================

class TestProfileExtra(UsersAPIExtraBase):
    """ProfileView 补充场景：avatar_url / phone 更新"""

    @pytest.mark.integration
    def test_patch_profile_avatar_and_phone(self):
        """用户可更新自己的头像与手机号（ProfileUpdateSerializer 允许字段）"""
        resp = self.client.patch(
            '/api/v1/auth/profile/',
            data=json.dumps({'avatar_url': 'http://x/avatar.png', 'phone': '13800138000'}),
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 200
        self.normal_user.refresh_from_db()
        assert self.normal_user.avatar_url == 'http://x/avatar.png'
        assert self.normal_user.phone == '13800138000'


# ============================================================================
# ResetPasswordView —— 修改密码（多种校验分支）
# ============================================================================

class TestResetPasswordView(UsersAPIExtraBase):
    """ResetPasswordView：旧密码校验 + 复杂度校验"""

    @pytest.mark.integration
    def test_reset_password_success(self):
        """正确旧密码 + 符合复杂度的新密码 → 200，密码实际生效"""
        resp = self.client.post(
            '/api/v1/auth/reset-password/',
            data=json.dumps({'old_password': 'pass12345', 'new_password': 'Newpass123'}),
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 200
        self.normal_user.refresh_from_db()
        assert self.normal_user.check_password('Newpass123')
        assert self.normal_user.password_changed_at is not None

    @pytest.mark.integration
    def test_reset_password_too_short_400(self):
        """新密码少于 8 位 → 400"""
        resp = self.client.post(
            '/api/v1/auth/reset-password/',
            data=json.dumps({'old_password': 'pass12345', 'new_password': 'Ab1'}),
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_reset_password_same_as_old_400(self):
        """新密码与旧密码相同 → 400"""
        resp = self.client.post(
            '/api/v1/auth/reset-password/',
            data=json.dumps({'old_password': 'pass12345', 'new_password': 'pass12345'}),
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_reset_password_no_uppercase_400(self):
        """新密码缺大写字母 → 400"""
        resp = self.client.post(
            '/api/v1/auth/reset-password/',
            data=json.dumps({'old_password': 'pass12345', 'new_password': 'newpass123'}),
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_reset_password_wrong_old_password_400(self):
        """旧密码错误 → 400"""
        resp = self.client.post(
            '/api/v1/auth/reset-password/',
            data=json.dumps({'old_password': 'wrong_old', 'new_password': 'Newpass123'}),
            content_type='application/json',
            **self.normal_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_reset_password_anonymous_401(self):
        """匿名修改密码 → 401"""
        resp = self.client.post('/api/v1/auth/reset-password/')
        assert resp.status_code in (401, 403)


# ============================================================================
# PasswordResetRequestView —— 邮箱验证码发送（mock 验证码/Redis/邮件）
# ============================================================================

class TestPasswordResetRequestView(UsersAPIExtraBase):
    """PasswordResetRequestView：验证码生成与邮件发送"""

    @patch('apps.security.views.verify_captcha', return_value=True)
    @patch('apps.security.views._get_redis')
    @patch('django.core.mail.send_mail')
    @pytest.mark.integration
    def test_request_success_sends_email(self, mock_mail, mock_redis, _mock_captcha):
        """已注册邮箱 + 验证码通过 → 生成验证码写入 Redis 并发送邮件"""
        fake_redis = FakeRedis()
        mock_redis.return_value = fake_redis
        resp = self.client.post(
            '/api/v1/auth/password-reset/request/',
            data=json.dumps({'email': 'normal@test.com', 'captcha_id': 'c1', 'captcha_code': 'x'}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        assert resp.json()['ok'] is True
        mock_mail.assert_called_once()
        # 验证码已写入 Redis（key: pwd_reset:{email}，6 位数字）
        stored = fake_redis.data.get('pwd_reset:normal@test.com')
        assert stored and len(stored) == 6 and stored.isdigit()

    @pytest.mark.integration
    def test_request_missing_email_400(self):
        """未传邮箱 → 400"""
        resp = self.client.post(
            '/api/v1/auth/password-reset/request/',
            data=json.dumps({'captcha_id': 'c1', 'captcha_code': 'x'}),
            content_type='application/json',
        )
        assert resp.status_code == 400

    @patch('apps.security.views.verify_captcha', return_value=False)
    @pytest.mark.integration
    def test_request_captcha_fail_400(self, _mock_captcha):
        """图形验证码错误 → 400（防接口被刷）"""
        resp = self.client.post(
            '/api/v1/auth/password-reset/request/',
            data=json.dumps({'email': 'normal@test.com', 'captcha_id': 'c1', 'captcha_code': 'bad'}),
            content_type='application/json',
        )
        assert resp.status_code == 400

    @patch('apps.security.views.verify_captcha', return_value=True)
    @patch('apps.security.views._get_redis')
    @patch('django.core.mail.send_mail')
    @pytest.mark.integration
    def test_request_rate_limit_429(self, mock_mail, mock_redis, _mock_captcha):
        """1 分钟内重复请求 → 429（Redis 中已有验证码且 TTL > 240s）"""
        fake_redis = FakeRedis({'pwd_reset:normal@test.com': '123456'})
        mock_redis.return_value = fake_redis
        resp = self.client.post(
            '/api/v1/auth/password-reset/request/',
            data=json.dumps({'email': 'normal@test.com', 'captcha_id': 'c1', 'captcha_code': 'x'}),
            content_type='application/json',
        )
        assert resp.status_code == 429
        mock_mail.assert_not_called()

    @patch('apps.security.views.verify_captcha', return_value=True)
    @patch('apps.security.views._get_redis', return_value=None)
    @patch('django.core.mail.send_mail')
    @pytest.mark.integration
    def test_request_email_not_found_still_ok(self, mock_mail, mock_redis, _mock_captcha):
        """邮箱不存在也返回成功（防邮箱探测）；Redis 不可用时静默降级不发邮件"""
        resp = self.client.post(
            '/api/v1/auth/password-reset/request/',
            data=json.dumps({'email': 'nobody@test.com', 'captcha_id': 'c1', 'captcha_code': 'x'}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        mock_mail.assert_not_called()


# ============================================================================
# PasswordResetConfirmView —— 验证码校验 + 重置密码（mock Redis）
# ============================================================================

class TestPasswordResetConfirmView(UsersAPIExtraBase):
    """PasswordResetConfirmView：验证码一次性校验 + 新密码生效"""

    def _confirm(self, email='normal@test.com', code='123456', new_password='Newpass123'):
        """POST 密码重置确认的公共请求"""
        return self.client.post(
            '/api/v1/auth/password-reset/confirm/',
            data=json.dumps({'email': email, 'code': code, 'new_password': new_password}),
            content_type='application/json',
        )

    @patch('apps.security.views._get_redis')
    @pytest.mark.integration
    def test_confirm_success_resets_password(self, mock_redis):
        """验证码正确 → 密码重置成功，验证码被删除（一次性使用）"""
        fake_redis = FakeRedis({'pwd_reset:normal@test.com': '123456'})
        mock_redis.return_value = fake_redis
        resp = self._confirm()
        assert resp.status_code == 200
        self.normal_user.refresh_from_db()
        assert self.normal_user.check_password('Newpass123')
        assert 'pwd_reset:normal@test.com' not in fake_redis.data

    @patch('apps.security.views._get_redis')
    @pytest.mark.integration
    def test_confirm_wrong_code_400(self, mock_redis):
        """验证码错误 → 400，密码不变"""
        mock_redis.return_value = FakeRedis({'pwd_reset:normal@test.com': '123456'})
        resp = self._confirm(code='999999')
        assert resp.status_code == 400
        self.normal_user.refresh_from_db()
        assert self.normal_user.check_password('pass12345')

    @patch('apps.security.views._get_redis')
    @pytest.mark.integration
    def test_confirm_expired_code_400(self, mock_redis):
        """验证码过期（Redis 无记录）→ 400"""
        mock_redis.return_value = FakeRedis({})
        resp = self._confirm()
        assert resp.status_code == 400

    @patch('apps.security.views._get_redis', return_value=None)
    @pytest.mark.integration
    def test_confirm_redis_unavailable_500(self, mock_redis):
        """Redis 不可用 → 500（服务暂时不可用）"""
        resp = self._confirm()
        assert resp.status_code == 500

    @patch('apps.security.views._get_redis')
    @pytest.mark.integration
    def test_confirm_user_not_found_400(self, mock_redis):
        """验证码存在但邮箱无对应账号 → 400"""
        mock_redis.return_value = FakeRedis({'pwd_reset:ghost@test.com': '123456'})
        resp = self._confirm(email='ghost@test.com')
        assert resp.status_code == 400

    @patch('apps.security.views._get_redis')
    @pytest.mark.integration
    def test_confirm_missing_params_400(self, mock_redis):
        """缺邮箱 / 缺验证码 → 400"""
        mock_redis.return_value = FakeRedis({'pwd_reset:normal@test.com': '123456'})
        resp = self.client.post(
            '/api/v1/auth/password-reset/confirm/',
            data=json.dumps({'code': '123456', 'new_password': 'Newpass123'}),
            content_type='application/json',
        )
        assert resp.status_code == 400

    @patch('apps.security.views._get_redis')
    @pytest.mark.integration
    def test_confirm_weak_password_400(self, mock_redis):
        """新密码不满足复杂度（全小写）→ 400"""
        mock_redis.return_value = FakeRedis({'pwd_reset:normal@test.com': '123456'})
        resp = self._confirm(new_password='weakpass1')
        assert resp.status_code == 400


# ============================================================================
# UserViewSet 列表筛选 / 分页 / 数据范围过滤
# ============================================================================

