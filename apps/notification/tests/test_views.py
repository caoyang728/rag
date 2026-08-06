"""
apps.notification.views 接口集成测试 —— 订阅管理 & 发送记录 API 端点

覆盖范围：
- SubscriptionView GET：返回当前用户订阅状态（含 DEFAULT_SUBS 默认值补齐）
- SubscriptionView PUT：更新单个 category 订阅状态（update_or_create）
- SubscriptionView PATCH：批量更新多个 category（subscriptions 对象）
- SendLogView GET：返回最近 100 条发送记录（按 -id 倒序）
- 认证拦截：匿名访问 subscriptions / send-logs 均 401
- 用户隔离：A 用户订阅状态不影响 B 用户

采用 pytest-django（django_db）+ JWT：
视图走 IsAuthenticated 权限链路 + ORM update_or_create 读写，
需真实 DB + 真实 JWT 解析验证端到端契约；mock 会掩盖越权与隔离风险。
"""
import json

import pytest
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken

from apps.notification.models import EmailSubscription, EmailSendLog
from apps.users.models import User


# ----------------------------------------------------------------------------
# 复用 users/tests 的用户构造模式：超管走 role_key='super_admin' 系统级快路径
# ----------------------------------------------------------------------------
def _create_test_user(username, password='testpass123', is_super_admin=False):
    """创建测试用户，is_super_admin 时授予 super_admin 角色

    is_super_admin 是 User 模型的 @property（基于 super_admin 角色判定），
    故必须通过 UserRoleRel 授予角色，不能直接写字段。
    """
    from apps.users.models import Role, UserRoleRel, GrantStatus
    user = User.objects.create_user(
        username=username, email=f'{username}@test.com', password=password)
    if is_super_admin:
        admin_role = Role.objects.get_or_create(
            role_key='super_admin',
            defaults=dict(name='超级管理员', is_builtin=True))[0]
        UserRoleRel.objects.get_or_create(
            user=user, role=admin_role, defaults={'status': GrantStatus.ACTIVE})
    return user


def _get_auth_token(user):
    """生成 JWT access token"""
    return str(RefreshToken.for_user(user).access_token)


@pytest.mark.django_db
class NotificationViewsTestBase:
    """通知视图测试公共基类 —— 准备超管/普通用户 + JWT header（子类自动继承 django_db）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入 client/双用户 + JWT header"""
        self.client = Client()
        self.user_a = _create_test_user('user_a')
        self.user_b = _create_test_user('user_b')

        self.headers_a = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.user_a)}'}
        self.headers_b = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.user_b)}'}
        self.anon_headers = {}


# ============================================================================
# SubscriptionView GET —— 订阅状态查看
# ============================================================================
class TestSubscriptionViewGet(NotificationViewsTestBase):
    """SubscriptionView GET 接口测试"""

    @pytest.mark.integration
    def test_get_returns_default_subs_for_new_user(self):
        """新用户无订阅记录时，GET 应返回 DEFAULT_SUBS 全部类别及其默认值"""
        resp = self.client.get('/api/v1/notification/subscriptions/', **self.headers_a)
        assert resp.status_code == 200
        data = resp.json()
        subs = data['subscriptions']
        # DEFAULT_SUBS 包含 4 个类别
        assert set(subs.keys()) == {'node_update', 'system_notice',
                                    'daily_report', 'keyword_alert'}
        # 默认值：node_update/system_notice/keyword_alert 默认 True，daily_report 默认 False
        assert subs['node_update']['is_enabled'] is True
        assert subs['system_notice']['is_enabled'] is True
        assert subs['keyword_alert']['is_enabled'] is True
        assert subs['daily_report']['is_enabled'] is False
        # 每项都应带 label
        assert subs['node_update']['label'] == '知识库节点更新'

    @pytest.mark.integration
    def test_get_returns_user_actual_subs(self):
        """已存在订阅记录时，GET 应返回用户实际状态而非默认值"""
        EmailSubscription.objects.create(
            user=self.user_a, category='daily_report', is_enabled=True)
        EmailSubscription.objects.create(
            user=self.user_a, category='system_notice', is_enabled=False)

        resp = self.client.get('/api/v1/notification/subscriptions/', **self.headers_a)
        assert resp.status_code == 200
        subs = resp.json()['subscriptions']
        # 实际状态覆盖默认值
        assert subs['daily_report']['is_enabled'] is True
        assert subs['system_notice']['is_enabled'] is False
        # 未配置的类别仍回退默认值
        assert subs['node_update']['is_enabled'] is True

    @pytest.mark.integration
    def test_get_user_isolation(self):
        """A 用户的订阅状态不应影响 B 用户"""
        EmailSubscription.objects.create(
            user=self.user_a, category='daily_report', is_enabled=True)

        resp_a = self.client.get('/api/v1/notification/subscriptions/', **self.headers_a)
        resp_b = self.client.get('/api/v1/notification/subscriptions/', **self.headers_b)
        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
        # A 已开启 daily_report，B 仍为默认关闭
        assert resp_a.json()['subscriptions']['daily_report']['is_enabled'] is True
        assert resp_b.json()['subscriptions']['daily_report']['is_enabled'] is False

    @pytest.mark.integration
    def test_get_anonymous_401(self):
        """匿名用户访问订阅列表应 401（IsAuthenticated 拦截）"""
        resp = self.client.get('/api/v1/notification/subscriptions/', **self.anon_headers)
        assert resp.status_code in [401, 403]


# ============================================================================
# SubscriptionView PUT —— 单个类别更新
# ============================================================================
class TestSubscriptionViewPut(NotificationViewsTestBase):
    """SubscriptionView PUT 接口测试"""

    @pytest.mark.integration
    def test_put_creates_new_subscription(self):
        """PUT 对未存在的 category 应创建订阅记录"""
        resp = self.client.put(
            '/api/v1/notification/subscriptions/',
            data=json.dumps({'category': 'daily_report', 'is_enabled': True}),
            content_type='application/json',
            **self.headers_a)
        assert resp.status_code == 200
        body = resp.json()
        assert body['ok'] is True
        assert body['category'] == 'daily_report'
        assert body['is_enabled'] is True
        # 记录应落库
        sub = EmailSubscription.objects.get(user=self.user_a, category='daily_report')
        assert sub.is_enabled is True

    @pytest.mark.integration
    def test_put_updates_existing_subscription(self):
        """PUT 对已存在的 category 应更新状态（update_or_create）"""
        EmailSubscription.objects.create(
            user=self.user_a, category='system_notice', is_enabled=True)

        resp = self.client.put(
            '/api/v1/notification/subscriptions/',
            data=json.dumps({'category': 'system_notice', 'is_enabled': False}),
            content_type='application/json',
            **self.headers_a)
        assert resp.status_code == 200
        assert resp.json()['is_enabled'] is False
        # 同一 user+category 不应新增第二条
        assert EmailSubscription.objects.filter(
            user=self.user_a, category='system_notice').count() == 1
        sub = EmailSubscription.objects.get(user=self.user_a, category='system_notice')
        assert sub.is_enabled is False

    @pytest.mark.integration
    def test_put_default_is_enabled_true(self):
        """PUT 未传 is_enabled 时应默认为 True"""
        resp = self.client.put(
            '/api/v1/notification/subscriptions/',
            data=json.dumps({'category': 'keyword_alert'}),
            content_type='application/json',
            **self.headers_a)
        assert resp.status_code == 200
        assert resp.json()['is_enabled'] is True

    @pytest.mark.integration
    def test_put_missing_category_400(self):
        """PUT 缺少 category 应返回 400"""
        resp = self.client.put(
            '/api/v1/notification/subscriptions/',
            data=json.dumps({'is_enabled': True}),
            content_type='application/json',
            **self.headers_a)
        assert resp.status_code == 400
        assert 'category' in resp.json()['detail']

    @pytest.mark.integration
    def test_put_anonymous_401(self):
        """匿名用户 PUT 应 401"""
        resp = self.client.put(
            '/api/v1/notification/subscriptions/',
            data=json.dumps({'category': 'daily_report', 'is_enabled': True}),
            content_type='application/json',
            **self.anon_headers)
        assert resp.status_code in [401, 403]


# ============================================================================
# SubscriptionView PATCH —— 批量更新
# ============================================================================
class TestSubscriptionViewPatch(NotificationViewsTestBase):
    """SubscriptionView PATCH 批量更新接口测试"""

    @pytest.mark.integration
    def test_patch_batch_update_multiple(self):
        """PATCH 一次更新多个 category，返回 updated 列表"""
        resp = self.client.patch(
            '/api/v1/notification/subscriptions/',
            data=json.dumps({'subscriptions': {
                'daily_report': True,
                'system_notice': False,
                'keyword_alert': True,
            }}),
            content_type='application/json',
            **self.headers_a)
        assert resp.status_code == 200
        body = resp.json()
        assert body['ok'] is True
        assert set(body['updated']) == {'daily_report', 'system_notice', 'keyword_alert'}
        # 逐条验证落库
        assert EmailSubscription.objects.get(
            user=self.user_a, category='daily_report').is_enabled is True
        assert EmailSubscription.objects.get(
            user=self.user_a, category='system_notice').is_enabled is False

    @pytest.mark.integration
    def test_patch_invalid_subscriptions_type_400(self):
        """subscriptions 非对象时应返回 400"""
        resp = self.client.patch(
            '/api/v1/notification/subscriptions/',
            data=json.dumps({'subscriptions': ['not', 'a', 'dict']}),
            content_type='application/json',
            **self.headers_a)
        assert resp.status_code == 400
        assert 'subscriptions' in resp.json()['detail']

    @pytest.mark.integration
    def test_patch_empty_subscriptions(self):
        """空 subscriptions 对象应返回 200，updated 为空列表"""
        resp = self.client.patch(
            '/api/v1/notification/subscriptions/',
            data=json.dumps({'subscriptions': {}}),
            content_type='application/json',
            **self.headers_a)
        assert resp.status_code == 200
        assert resp.json()['updated'] == []

    @pytest.mark.integration
    def test_patch_anonymous_401(self):
        """匿名用户 PATCH 应 401"""
        resp = self.client.patch(
            '/api/v1/notification/subscriptions/',
            data=json.dumps({'subscriptions': {'daily_report': True}}),
            content_type='application/json',
            **self.anon_headers)
        assert resp.status_code in [401, 403]


# ============================================================================
# SendLogView GET —— 发送记录查看
# ============================================================================
class TestSendLogViewGet(NotificationViewsTestBase):
    """SendLogView GET 接口测试"""

    @pytest.mark.integration
    def test_get_empty_logs(self):
        """无发送记录时返回空 rows 列表"""
        resp = self.client.get('/api/v1/notification/send-logs/', **self.headers_a)
        assert resp.status_code == 200
        data = resp.json()
        assert data['rows'] == []
        assert data['count'] == 0

    @pytest.mark.integration
    def test_get_returns_recent_logs_desc(self):
        """GET 应返回最近 100 条记录，按 -id 倒序排列"""
        for i in range(3):
            EmailSendLog.objects.create(
                to_email=f'dest{i}@test.com',
                subject=f'邮件{i}',
                status='success')

        resp = self.client.get('/api/v1/notification/send-logs/', **self.headers_a)
        assert resp.status_code == 200
        data = resp.json()
        assert data['count'] == 3
        # 倒序：最后创建的（id 最大）排在最前
        assert data['rows'][0]['subject'] == '邮件2'
        assert data['rows'][2]['subject'] == '邮件0'

    @pytest.mark.integration
    def test_get_includes_expected_fields(self):
        """返回字段应包含 id/to_email/subject/category/status/error_message/created_at"""
        EmailSendLog.objects.create(
            to_email='fields@test.com', subject='字段校验',
            category='system_notice', status='failed',
            error_message='timeout')

        resp = self.client.get('/api/v1/notification/send-logs/', **self.headers_a)
        row = resp.json()['rows'][0]
        for field in ('id', 'to_email', 'subject', 'category',
                      'status', 'error_message', 'created_at'):
            assert field in row
        assert row['to_email'] == 'fields@test.com'
        assert row['status'] == 'failed'
        assert row['error_message'] == 'timeout'

    @pytest.mark.integration
    def test_get_capped_at_100(self):
        """超过 100 条时只返回最近 100 条（[:100] 切片）"""
        for i in range(120):
            EmailSendLog.objects.create(
                to_email=f'cap{i}@test.com', subject=f'批量{i}')

        resp = self.client.get('/api/v1/notification/send-logs/', **self.headers_a)
        assert resp.status_code == 200
        assert resp.json()['count'] == 100

    @pytest.mark.integration
    def test_get_anonymous_401(self):
        """匿名用户访问发送记录应 401"""
        resp = self.client.get('/api/v1/notification/send-logs/', **self.anon_headers)
        assert resp.status_code in [401, 403]
