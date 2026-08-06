"""
apps.audit.middleware 测试 —— 审计中间件

覆盖范围：
- _match_action：URL 模式匹配（含 target_id 捕获组）/ 未匹配
- 请求落库：POST 到审计路径写 AuditLog、JWT/请求体两种用户名来源
- 响应状态映射：200->success / 403->denied / 500->failed
- 路径过滤：GET 方法、非 /api/v1/ 路径、未匹配规则路径均不落库

用 RequestFactory（django_db 集成测试）：
process_response 内部走 AuditLog.objects.create 触发哈希链 save，需真实事务；
RequestFactory 构造轻量请求绕过认证/路由，专注验证中间件分支。
_get_user_from_jwt 统一 patch，避免依赖真实 JWT 与 User 查询。
"""
import json
from unittest.mock import patch

import pytest
from django.test import RequestFactory
from django.http import HttpResponse

from apps.audit.middleware import AuditMiddleware
from apps.audit.models import AuditLog


class TestMatchAction:
    """_match_action URL 规则匹配纯逻辑测试（无 DB）"""

    @pytest.fixture(autouse=True)
    def _mw(self):
        """pytest fixture：注入中间件实例（get_response 为占位 callable）"""
        # Django 5.2 MiddlewareMixin 强制要求 get_response 参数；
        # 这里只测 _match_action 纯逻辑，不触发 __call__，故传占位 callable 即可
        self.mw = AuditMiddleware(get_response=lambda req: None)

    @pytest.mark.unit
    def test_match_login(self):
        """登录路径匹配 login 动作，无 target_id"""
        action, cat, target_type, target_id = self.mw._match_action('/api/v1/auth/login/')
        assert action == 'login'
        assert cat == 'auth'
        assert target_type == 'auth'
        assert target_id == ''

    @pytest.mark.unit
    def test_match_delete_document_captures_id(self):
        """带数字捕获组的路径应提取 target_id"""
        action, cat, target_type, target_id = self.mw._match_action('/api/v1/documents/123/')
        assert action == 'delete_document'
        assert cat == 'document'
        assert target_type == 'document'
        assert target_id == '123'

    @pytest.mark.unit
    def test_match_user_toggle_status(self):
        """toggle_status 子路径匹配并捕获用户 id"""
        action, cat, target_type, target_id = self.mw._match_action('/api/v1/users/42/toggle_status/')
        assert action == 'toggle_user_status'
        assert target_id == '42'

    @pytest.mark.unit
    def test_no_match_unknown_path(self):
        """未命中任何规则的路径返回 (None, None, '', '')"""
        action, cat, target_type, target_id = self.mw._match_action('/api/v1/unknown/foo/')
        assert action is None
        assert cat is None
        assert target_type == ''
        assert target_id == ''


@pytest.mark.django_db
class TestAuditMiddlewareLogging:
    """process_response 请求落库与状态映射集成测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入 RequestFactory 与中间件实例"""
        self.factory = RequestFactory()
        # get_response 占位：process_response 测试不经过 __call__
        self.mw = AuditMiddleware(get_response=lambda req: None)

    @patch('apps.audit.middleware._get_user_from_jwt', return_value=(None, 'loginuser'))
    def test_login_request_logged(self, _mock_jwt):
        """POST 登录路径应写一条 AuditLog，结果为 success"""
        request = self.factory.post(
            '/api/v1/auth/login/',
            data={'username': 'loginuser'},
            content_type='application/json',
            HTTP_USER_AGENT='test-agent',
        )
        response = HttpResponse(status=200)

        self.mw.process_response(request, response)

        log = AuditLog.objects.filter(action='login').first()
        assert log is not None
        assert log.action_category == 'auth'
        assert log.result == 'success'
        assert log.actor_username == 'loginuser'
        assert log.method == 'POST'
        assert log.path == '/api/v1/auth/login/'
        assert log.user_agent == 'test-agent'

    @patch('apps.audit.middleware._get_user_from_jwt', return_value=(None, ''))
    def test_username_falls_back_to_body(self, _mock_jwt):
        """JWT 无用户名时从 request.body 提取 username（登录等无 JWT 场景）"""
        request = self.factory.post(
            '/api/v1/auth/login/',
            data=json.dumps({'username': 'bodyuser'}),
            content_type='application/json',
        )
        response = HttpResponse(status=200)

        self.mw.process_response(request, response)

        log = AuditLog.objects.filter(action='login').first()
        assert log is not None
        assert log.actor_username == 'bodyuser'

    @patch('apps.audit.middleware._get_user_from_jwt', return_value=(None, 'admin'))
    def test_result_success_for_2xx(self, _mock_jwt):
        """响应 < 400 时 result=success"""
        request = self.factory.post(
            '/api/v1/auth/login/', data={}, content_type='application/json')
        self.mw.process_response(request, HttpResponse(status=200))
        assert AuditLog.objects.filter(action='login', result='success').exists()

    @patch('apps.audit.middleware._get_user_from_jwt', return_value=(None, 'admin'))
    def test_result_denied_for_403(self, _mock_jwt):
        """响应 403 时 result=denied（优先于 failed 分支）"""
        request = self.factory.post(
            '/api/v1/auth/login/', data={}, content_type='application/json')
        self.mw.process_response(request, HttpResponse(status=403))
        assert AuditLog.objects.filter(action='login', result='denied').exists()

    @patch('apps.audit.middleware._get_user_from_jwt', return_value=(None, 'admin'))
    def test_result_failed_for_500(self, _mock_jwt):
        """响应 >= 400（非 403）时 result=failed"""
        request = self.factory.post(
            '/api/v1/auth/login/', data={}, content_type='application/json')
        self.mw.process_response(request, HttpResponse(status=500))
        assert AuditLog.objects.filter(action='login', result='failed').exists()


@pytest.mark.django_db
class TestAuditMiddlewareFiltering:
    """process_response 路径/方法过滤测试（不应落库的场景）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入 RequestFactory 与中间件实例"""
        self.factory = RequestFactory()
        # get_response 占位：process_response 测试不经过 __call__
        self.mw = AuditMiddleware(get_response=lambda req: None)

    @pytest.mark.integration
    def test_get_method_not_logged(self):
        """GET 请求不在审计方法集合内，不落库"""
        request = self.factory.get('/api/v1/auth/login/')
        response = HttpResponse(status=200)

        self.mw.process_response(request, response)

        assert AuditLog.objects.filter(action='login').count() == 0

    @pytest.mark.integration
    def test_non_api_path_not_logged(self):
        """非 /api/v1/ 前缀路径不落库"""
        request = self.factory.post('/admin/something/', data={}, content_type='application/json')
        response = HttpResponse(status=200)

        self.mw.process_response(request, response)

        assert AuditLog.objects.count() == 0

    @pytest.mark.integration
    def test_unmatched_api_path_not_logged(self):
        """未命中 _ACTION_MAP 的 /api/v1/ 路径不落库"""
        request = self.factory.post('/api/v1/unknown/foo/', data={}, content_type='application/json')
        response = HttpResponse(status=200)

        self.mw.process_response(request, response)

        assert AuditLog.objects.count() == 0

    @patch('apps.audit.middleware._get_user_from_jwt', return_value=(None, 'admin'))
    def test_delete_document_captures_target_id(self, _mock_jwt):
        """DELETE 文档路径应落库且 target_id 来自正则捕获组"""
        request = self.factory.delete('/api/v1/documents/99/')
        response = HttpResponse(status=204)

        self.mw.process_response(request, response)

        log = AuditLog.objects.filter(action='delete_document').first()
        assert log is not None
        assert log.target_type == 'document'
        assert log.target_id == '99'
        assert log.method == 'DELETE'


@pytest.fixture
def factory():
    """RequestFactory 实例（构造无中间件请求对象）"""
    return RequestFactory()


class TestAuditMiddlewareHelpers:
    """审计中间件模块级辅助函数测试（_get_user_from_jwt / _get_username_from_body / _get_ip）"""

    # ---- _get_ip ----
    @pytest.mark.unit
    def test_get_ip_uses_first_x_forwarded_for(self, factory):
        """X-Forwarded-For 多级代理时取第一个 IP"""
        from apps.audit.middleware import _get_ip
        request = factory.get('/x')
        request.META['HTTP_X_FORWARDED_FOR'] = '203.0.113.1, 10.0.0.1'
        assert _get_ip(request) == '203.0.113.1'

    @pytest.mark.unit
    def test_get_ip_falls_back_to_remote_addr(self, factory):
        """无 X-Forwarded-For 时回退 REMOTE_ADDR"""
        from apps.audit.middleware import _get_ip
        request = factory.get('/x')
        assert _get_ip(request) == '127.0.0.1'

    # ---- _get_username_from_body ----
    @pytest.mark.unit
    def test_get_username_from_body_bytes(self, factory):
        """bytes 类型的 body 也应能解析出 username"""
        from apps.audit.middleware import _get_username_from_body
        request = factory.post(
            '/api/v1/auth/login/',
            data=json.dumps({'username': 'bytuser'}),
            content_type='application/json')
        assert _get_username_from_body(request) == 'bytuser'

    @pytest.mark.unit
    def test_get_username_from_body_invalid_json(self, factory):
        """非法 JSON body 静默返回空串，不抛异常"""
        from apps.audit.middleware import _get_username_from_body
        request = factory.post(
            '/api/v1/auth/login/', data=b'not-json', content_type='application/json')
        assert _get_username_from_body(request) == ''

    @pytest.mark.unit
    def test_get_username_from_body_empty(self, factory):
        """无 body 时返回空串"""
        from apps.audit.middleware import _get_username_from_body
        request = factory.post('/api/v1/auth/login/')
        assert _get_username_from_body(request) == ''

    # ---- _get_user_from_jwt ----
    @pytest.mark.unit
    def test_get_user_from_jwt_no_header(self, factory):
        """无 Authorization header 时返回 (None, '')"""
        from apps.audit.middleware import _get_user_from_jwt
        request = factory.get('/x')
        assert _get_user_from_jwt(request) == (None, '')

    @pytest.mark.unit
    def test_get_user_from_jwt_invalid_token(self, factory):
        """非法 JWT 时返回 (None, '')，不抛异常"""
        from apps.audit.middleware import _get_user_from_jwt
        request = factory.get('/x', HTTP_AUTHORIZATION='Bearer invalid.token.value')
        assert _get_user_from_jwt(request) == (None, '')

    @pytest.mark.django_db
    @pytest.mark.integration
    def test_get_user_from_jwt_valid_token(self, factory):
        """合法 JWT 返回 user_id 与 username"""
        from rest_framework_simplejwt.tokens import RefreshToken
        from apps.users.models import User
        from apps.audit.middleware import _get_user_from_jwt

        user = User.objects.create_user(username='jwter', email='jwter@test.com', password='x')
        token = str(RefreshToken.for_user(user).access_token)
        request = factory.get('/x', HTTP_AUTHORIZATION=f'Bearer {token}')

        uid, username = _get_user_from_jwt(request)
        assert uid == user.id
        assert username == 'jwter'
