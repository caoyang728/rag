"""
apps.users.views 接口集成测试 —— 用户/角色/权限 API 端点

覆盖范围：
- UserViewSet CRUD：list / create / retrieve / update / delete（软删）
- 认证与权限：匿名 401、普通用户无 user.manage 权限 403、超管放行
- UserViewSet 自定义 action：toggle_status（禁用/启用）、batch_delete（批量软删）
- LoginView：登录成功返回 JWT、密码错误 401、验证码错误 401
- ProfileView：已登录可查看个人资料、匿名 401

接口涉及 RBAC 权限判定（CanManageUsers）、ORM 写入与软删，
需真实 DB + 真实权限链路验证端到端契约，mock 会掩盖权限闭环漏洞。
"""
import json
from unittest.mock import patch

import pytest
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken

from apps.users.models import (
    User, Role, Department, Team, UserRoleRel, GrantStatus,
)


def _get_or_create_role(role_key, **defaults):
    """获取或创建内置角色，补齐默认字段"""
    default_map = {
        'super_admin': dict(name='超级管理员', is_builtin=True),
        'viewer': dict(name='查看者', is_builtin=True),
        'contributor': dict(name='贡献者', is_builtin=True),
    }
    defaults = {**default_map.get(role_key, {}), **defaults}
    role, _ = Role.objects.get_or_create(role_key=role_key, defaults=defaults)
    return role


def _create_test_user(username, password='testpass123', is_super_admin=False, **extra):
    """创建测试用户，可选绑定 super_admin 角色

    email 可通过 extra 覆盖默认值 {username}@test.com
    """
    extra.setdefault('email', f'{username}@test.com')
    user = User.objects.create_user(
        username=username, password=password, **extra)
    if is_super_admin:
        admin_role = _get_or_create_role('super_admin')
        UserRoleRel.objects.get_or_create(
            user=user, role=admin_role,
            defaults={'status': GrantStatus.ACTIVE})
    return user


def _get_auth_token(user):
    """生成 JWT access token"""
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token)


@pytest.mark.django_db
class UsersAPITestBase:
    """用户 API 测试公共基类 —— 准备超管/普通用户/部门/团队 + JWT header（子类自动继承 django_db）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入 client/超管/普通用户/部门/团队 + JWT header"""
        self.client = Client()
        # 预建 viewer 角色（UserSerializer 兜底展示依赖）
        _get_or_create_role('viewer')
        _get_or_create_role('contributor')

        self.super_admin = _create_test_user(
            username='admin', password='admin12345', is_super_admin=True)
        self.normal_user = _create_test_user(
            username='normal', password='pass12345', is_super_admin=False)

        # 部门 + 团队（用户创建/更新接口可能引用）
        self.dept = Department.objects.create(name='测试部', code='test_dept')
        self.team = Team.objects.create(name='测试组', code='test_team', department=self.dept)

        self.anon_headers = {}
        self.normal_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.normal_user)}'}
        self.admin_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.super_admin)}'}


# ============================================================================
# UserViewSet CRUD —— 超管视角的增删改查
# ============================================================================
class TestUserViewSetCRUD(UsersAPITestBase):
    """UserViewSet 增删改查接口测试（超管权限）"""

    @pytest.mark.integration
    def test_list_superuser_200(self):
        """超管可查看用户列表"""
        resp = self.client.get('/api/v1/auth/users/', **self.admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        results = data['results'] if 'results' in data else data
        # 至少包含超管与普通用户
        assert len(results) >= 2

    @pytest.mark.integration
    def test_list_anonymous_401(self):
        """匿名用户访问用户列表应 401（IsAuthenticated 拦截）"""
        resp = self.client.get('/api/v1/auth/users/', **self.anon_headers)
        assert resp.status_code in [401, 403]

    @pytest.mark.integration
    def test_list_normal_user_without_manage_perm_403(self):
        """普通用户无 user.manage 权限应 403（CanManageUsers 拦截）"""
        resp = self.client.get('/api/v1/auth/users/', **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_create_user_201(self):
        """超管创建用户应返回 201，用户落库"""
        resp = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({
                'username': 'newuser',
                'email': 'newuser@test.com',
                'real_name': '新用户',
                'department_id': self.dept.id,
                'status': 'active',
            }),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data['username'] == 'newuser'
        assert data['email'] == 'newuser@test.com'
        assert User.objects.filter(username='newuser', is_deleted=False).exists()

    @pytest.mark.integration
    def test_create_user_normal_user_403(self):
        """普通用户无用户管理权限创建用户应 403"""
        resp = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({'username': 'x', 'email': 'x@test.com'}),
            content_type='application/json',
            **self.normal_headers
        )
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_create_duplicate_email_revivable_409(self):
        """邮箱命中已软删用户应返回 409 + USER_REVIVABLE（询问恢复）"""
        # 创建后软删一个用户
        deleted = _create_test_user(username='deleted_one', email='dup@test.com')
        deleted.is_deleted = True
        deleted.deleted_at = deleted.created_at
        deleted.save()
        resp = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({'username': 'newname', 'email': 'dup@test.com'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 409
        data = resp.json()
        assert data.get('code') == 'USER_REVIVABLE'

    @pytest.mark.integration
    def test_retrieve_user_200(self):
        """超管可查看单个用户详情"""
        resp = self.client.get(
            f'/api/v1/auth/users/{self.normal_user.id}/', **self.admin_headers)
        assert resp.status_code == 200
        assert resp.json()['id'] == self.normal_user.id

    @pytest.mark.integration
    def test_update_user_200(self):
        """超管编辑用户信息应返回 200"""
        resp = self.client.patch(
            f'/api/v1/auth/users/{self.normal_user.id}/',
            data=json.dumps({'real_name': '修改后的名字'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 200
        self.normal_user.refresh_from_db()
        assert self.normal_user.real_name == '修改后的名字'

    @pytest.mark.integration
    def test_delete_user_soft_204(self):
        """删除用户为软删（is_deleted=True），返回 204"""
        target = _create_test_user(username='to_delete', email='del@test.com')
        resp = self.client.delete(
            f'/api/v1/auth/users/{target.id}/', **self.admin_headers)
        assert resp.status_code == 204
        target.refresh_from_db()
        assert target.is_deleted is True
        assert target.status == 'disabled'

    @pytest.mark.integration
    def test_delete_super_admin_forbidden(self):
        """超级管理员不能被删除（系统级快路径保护，防止锁死管理入口）"""
        resp = self.client.delete(
            f'/api/v1/auth/users/{self.super_admin.id}/', **self.admin_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_delete_self_forbidden(self):
        """不能删除自己（_check_can_manage_user 规则2）"""
        resp = self.client.delete(
            f'/api/v1/auth/users/{self.super_admin.id}/', **self.admin_headers)
        # 超管删自己：规则2（不能操作自己）或规则3（超管不能被禁用）拦截
        assert resp.status_code == 403


# ============================================================================
# UserViewSet 自定义 action —— toggle_status / batch_delete
# ============================================================================
class TestUserViewSetActions(UsersAPITestBase):
    """UserViewSet 自定义 action 测试"""

    @pytest.mark.integration
    def test_toggle_status_disable_then_enable(self):
        """禁用后再启用状态应正确切换"""
        target = _create_test_user(username='toggle_target', email='tog@test.com')
        # 禁用
        resp = self.client.post(
            f'/api/v1/auth/users/{target.id}/toggle_status/', **self.admin_headers)
        assert resp.status_code == 200
        target.refresh_from_db()
        assert target.status == 'disabled'
        # 启用
        resp = self.client.post(
            f'/api/v1/auth/users/{target.id}/toggle_status/', **self.admin_headers)
        assert resp.status_code == 200
        target.refresh_from_db()
        assert target.status == 'active'

    @pytest.mark.integration
    def test_batch_delete_multiple_users(self):
        """批量软删多个用户，返回删除数量"""
        u1 = _create_test_user(username='batch1', email='b1@test.com')
        u2 = _create_test_user(username='batch2', email='b2@test.com')
        resp = self.client.post(
            '/api/v1/auth/users/batch_delete/',
            data=json.dumps({'ids': [u1.id, u2.id]}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 200
        u1.refresh_from_db()
        u2.refresh_from_db()
        assert u1.is_deleted is True
        assert u2.is_deleted is True

    @pytest.mark.integration
    def test_batch_delete_empty_ids_400(self):
        """空 ids 列表应返回 400"""
        resp = self.client.post(
            '/api/v1/auth/users/batch_delete/',
            data=json.dumps({'ids': []}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_search_action_returns_users(self):
        """search action 应按关键词返回匹配用户"""
        _create_test_user(username='searchable_user', email='sch@test.com')
        resp = self.client.get(
            '/api/v1/auth/users/search/?q=searchable', **self.admin_headers)
        assert resp.status_code == 200
        users = resp.json()['users']
        assert any(u['username'] == 'searchable_user' for u in users)


# ============================================================================
# ProfileView —— 个人资料查看与修改
# ============================================================================
class TestProfileView(UsersAPITestBase):
    """ProfileView：个人资料查看/修改"""

    @pytest.mark.integration
    def test_get_profile_authenticated_200(self):
        """已登录用户可查看个人资料"""
        resp = self.client.get('/api/v1/auth/profile/', **self.normal_headers)
        assert resp.status_code == 200
        assert resp.json()['username'] == self.normal_user.username

    @pytest.mark.integration
    def test_get_profile_anonymous_401(self):
        """匿名用户查看个人资料应 401"""
        resp = self.client.get('/api/v1/auth/profile/', **self.anon_headers)
        assert resp.status_code in [401, 403]

    @pytest.mark.integration
    def test_patch_profile_update_real_name(self):
        """用户可修改自己的真实姓名"""
        resp = self.client.patch(
            '/api/v1/auth/profile/',
            data=json.dumps({'real_name': '新名字'}),
            content_type='application/json',
            **self.normal_headers
        )
        assert resp.status_code == 200
        self.normal_user.refresh_from_db()
        assert self.normal_user.real_name == '新名字'

    @pytest.mark.integration
    def test_patch_profile_email_not_changed(self):
        """企业邮箱不可自行修改：ProfileUpdateSerializer 未声明 email 字段，
        传入的 email 被 is_valid 静默丢弃，邮箱保持不变（安全保证）"""
        original_email = self.normal_user.email
        resp = self.client.patch(
            '/api/v1/auth/profile/',
            data=json.dumps({'email': 'changed@test.com'}),
            content_type='application/json',
            **self.normal_headers
        )
        # email 字段不在 serializer 中，被丢弃，请求成功但不改邮箱
        assert resp.status_code == 200
        self.normal_user.refresh_from_db()
        assert self.normal_user.email == original_email


# ============================================================================
# LoginView —— JWT 登录（mock 验证码）
# ============================================================================
class TestLoginView(UsersAPITestBase):
    """LoginView：登录认证接口（mock verify_captcha 避免依赖验证码基础设施）"""

    @patch('apps.security.views.verify_captcha', return_value=True)
    def test_login_success_returns_jwt(self, _mock_captcha):
        """正确的用户名密码 + 验证码通过 → 返回 access/refresh/user"""
        resp = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({
                'username': 'normal',
                'password': 'pass12345',
                'captcha_id': 'x',
                'captcha_code': 'x',
            }),
            content_type='application/json',
        )
        assert resp.status_code == 200
        data = resp.json()
        assert 'access' in data
        assert 'refresh' in data
        assert data['user']['username'] == 'normal'

    @patch('apps.security.views.verify_captcha', return_value=True)
    def test_login_wrong_password_401(self, _mock_captcha):
        """密码错误应返回 401"""
        resp = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({
                'username': 'normal',
                'password': 'wrong_password',
                'captcha_id': 'x',
                'captcha_code': 'x',
            }),
            content_type='application/json',
        )
        assert resp.status_code == 401

    @patch('apps.security.views.verify_captcha', return_value=False)
    def test_login_captcha_fail_401(self, _mock_captcha):
        """验证码错误应返回 401（不校验用户名密码）"""
        resp = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({
                'username': 'normal',
                'password': 'pass12345',
                'captcha_id': 'x',
                'captcha_code': 'wrong',
            }),
            content_type='application/json',
        )
        assert resp.status_code == 401

    def test_login_missing_credentials_400(self):
        """用户名或密码为空应返回 400"""
        resp = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'username': '', 'password': ''}),
            content_type='application/json',
        )
        assert resp.status_code == 400


# ============================================================================
# MyPermissionsView —— 个人权限查看
# ============================================================================
class TestMyPermissionsView(UsersAPITestBase):
    """MyPermissionsView：当前用户权限分组查看"""

    @pytest.mark.integration
    def test_get_my_permissions_authenticated_200(self):
        """已登录用户可查看自己的权限分组"""
        resp = self.client.get('/api/v1/auth/permissions/me/', **self.normal_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert 'roles' in data
        assert 'permission_groups' in data
        assert 'is_super_admin' in data

    @pytest.mark.integration
    def test_get_my_permissions_anonymous_401(self):
        """匿名用户查看权限应 401"""
        resp = self.client.get('/api/v1/auth/permissions/me/', **self.anon_headers)
        assert resp.status_code in [401, 403]

    @pytest.mark.integration
    def test_super_admin_permissions_flag(self):
        """超管的 is_super_admin 应为 True"""
        resp = self.client.get('/api/v1/auth/permissions/me/', **self.admin_headers)
        assert resp.status_code == 200
        assert resp.json()['is_super_admin'] is True
