"""
apps.security.views 接口集成测试 —— 安全管理 API 端点

覆盖范围：
- 敏感词 CRUD + choices 校验 + 正则校验 + 重复校验 + 长度校验
- IP 白名单 CRUD + 重复校验
- IP 黑名单 CRUD + 解封（is_active=False）+ update_or_create 语义
- 登录尝试列表 + 多维度过滤（result/username/ip）+ 分页
- 认证与权限：匿名 401、普通用户 403、超管放行（IsAdminUser → is_staff → is_super_admin）

采用 pytest-django（django_db）+ JWT：
接口使用 IsAdminUser 权限（判定 is_staff → is_super_admin → 查 UserRoleRel），
需真实 DB + 真实角色权限链路验证端到端契约，mock 会掩盖权限闭环漏洞。
"""
import json
from unittest.mock import patch

import pytest
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken

from apps.users.models import User, Role, UserRoleRel, GrantStatus
from apps.security import views as security_views
from apps.security.models import IpWhitelist, IpBlacklist, LoginAttempt, SensitiveWord


# ============================================================================
# 测试辅助函数
# ============================================================================

def _get_or_create_role(role_key, **defaults):
    """获取或创建内置角色，补齐默认字段

    super_admin 角色是 is_super_admin 判定的依据（查 UserRoleRel.role_key），
    测试中需确保角色存在才能让 is_staff 属性返回 True。
    """
    default_map = {
        'super_admin': dict(name='超级管理员', is_builtin=True),
        'viewer': dict(name='查看者', is_builtin=True),
    }
    defaults = {**default_map.get(role_key, {}), **defaults}
    role, _ = Role.objects.get_or_create(role_key=role_key, defaults=defaults)
    return role


def _create_test_user(username, password='testpass123', is_super_admin=False, **extra):
    """创建测试用户，可选绑定 super_admin 角色

    is_super_admin=True 时绑定 super_admin 角色（GrantStatus.ACTIVE），
    使 is_staff / is_super_admin 属性返回 True，通过 IsAdminUser 权限校验。
    """
    extra.setdefault('email', f'{username}@test.com')
    user = User.objects.create_user(username=username, password=password, **extra)
    if is_super_admin:
        admin_role = _get_or_create_role('super_admin')
        UserRoleRel.objects.get_or_create(
            user=user, role=admin_role,
            defaults={'status': GrantStatus.ACTIVE})
    return user


def _get_auth_token(user):
    """生成 JWT access token"""
    return str(RefreshToken.for_user(user).access_token)


# ============================================================================
# 测试基类
# ============================================================================

@pytest.mark.django_db
class SecurityAPITestBase:
    """安全 API 测试公共基类 —— 准备超管/普通用户 + JWT header（子类自动继承 django_db）

    所有安全接口使用 IsAdminUser 权限：
    - 匿名 → 401（NotAuthenticated）
    - 普通用户（is_staff=False）→ 403（PermissionDenied）
    - 超管（is_staff=True via super_admin 角色）→ 200
    """

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入 client/超管/普通用户 + JWT header"""
        self._init_env()

    def _init_env(self):
        """构造测试环境：client/角色/超管/普通用户 + JWT header（供子类复用）"""
        self.client = Client()
        # viewer 角色是 UserSerializer 兜底展示依赖，预建避免序列化报错
        _get_or_create_role('viewer')

        self.super_admin = _create_test_user(
            username='admin', password='admin12345', is_super_admin=True,
            real_name='管理员')
        self.normal_user = _create_test_user(
            username='normal', password='pass12345', is_super_admin=False,
            real_name='普通用户')

        self.anon_headers = {}
        self.normal_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.normal_user)}'}
        self.admin_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.super_admin)}'}


# ============================================================================
# 敏感词 CRUD —— SensitiveWordView / SensitiveWordDetailView
# ============================================================================
class TestSensitiveWordCRUD(SecurityAPITestBase):
    """敏感词 CRUD + choices 校验 + 正则校验 + 重复校验"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """在基类环境基础上 mock _trigger_reload

        Mock _trigger_reload 避免 SensitiveFilter 单例副作用（Redis 连接 / DB 重载）：
        词库变更触发的 force_reload 在测试中不应影响断言，且可能因 Redis 不可用产生噪音日志。
        以 yield 形态退出时自动恢复 patch。
        """
        self._init_env()
        with patch(
                'apps.security.views.SensitiveWordView._trigger_reload', return_value=None):
            yield

    @pytest.mark.integration
    def test_list_admin_200(self):
        """超管可查看敏感词列表"""
        SensitiveWord.objects.create(word='测试词', category='other', action='mask')
        resp = self.client.get('/api/v1/security/sensitive-words/', **self.admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['count'] >= 1
        assert any(r['word'] == '测试词' for r in data['rows'])

    @pytest.mark.integration
    def test_list_anonymous_401(self):
        """匿名用户访问敏感词列表应 401（IsAdminUser 拦截）"""
        resp = self.client.get('/api/v1/security/sensitive-words/', **self.anon_headers)
        assert resp.status_code in [401, 403]

    @pytest.mark.integration
    def test_list_normal_user_403(self):
        """普通用户（is_staff=False）访问敏感词列表应 403"""
        resp = self.client.get('/api/v1/security/sensitive-words/', **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_create_201(self):
        """超管创建敏感词返回 201，记录落库"""
        resp = self.client.post(
            '/api/v1/security/sensitive-words/',
            data=json.dumps({'word': '新敏感词', 'category': 'other', 'action': 'mask'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 201
        assert resp.json()['word'] == '新敏感词'
        assert SensitiveWord.objects.filter(word='新敏感词').exists()

    @pytest.mark.integration
    def test_create_empty_word_400(self):
        """word 必填，缺失时返回 400"""
        resp = self.client.post(
            '/api/v1/security/sensitive-words/',
            data=json.dumps({'category': 'other', 'action': 'mask'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_blank_word_400(self):
        """word 为纯空白时返回 400（strip 后为空，避免空格词污染词库）"""
        resp = self.client.post(
            '/api/v1/security/sensitive-words/',
            data=json.dumps({'word': '   ', 'category': 'other', 'action': 'mask'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_too_long_word_400(self):
        """word 超过 128 字符时返回 400（超长词拖慢 AC 自动机构建）"""
        resp = self.client.post(
            '/api/v1/security/sensitive-words/',
            data=json.dumps({'word': 'x' * 129, 'category': 'other', 'action': 'mask'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_invalid_action_400(self):
        """非法 action 返回 400（非法 action 会导致 block/mask/warn 分支全部失配）"""
        resp = self.client.post(
            '/api/v1/security/sensitive-words/',
            data=json.dumps({'word': '测试', 'category': 'other', 'action': 'invalid'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 400
        assert 'action' in resp.json()['detail']

    @pytest.mark.integration
    def test_create_invalid_category_400(self):
        """非法 category 返回 400"""
        resp = self.client.post(
            '/api/v1/security/sensitive-words/',
            data=json.dumps({'word': '测试', 'category': 'invalid_cat', 'action': 'mask'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 400
        assert 'category' in resp.json()['detail']

    @pytest.mark.integration
    def test_create_duplicate_400(self):
        """重复的 word 返回 400（word 字段 unique）"""
        SensitiveWord.objects.create(word='重复词', category='other', action='mask')
        resp = self.client.post(
            '/api/v1/security/sensitive-words/',
            data=json.dumps({'word': '重复词', 'category': 'other', 'action': 'mask'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 400
        assert '已存在' in resp.json()['detail']

    @pytest.mark.integration
    def test_create_invalid_regex_400(self):
        """is_regex=True 但正则非法时返回 400（提前校验给即时反馈，避免静默跳过）"""
        resp = self.client.post(
            '/api/v1/security/sensitive-words/',
            data=json.dumps({'word': '[invalid', 'category': 'other', 'action': 'mask', 'is_regex': True}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 400
        assert '正则' in resp.json()['detail']

    @pytest.mark.integration
    def test_update_action_200(self):
        """超管修改敏感词 action 返回 200"""
        sw = SensitiveWord.objects.create(word='编辑词', category='other', action='mask')
        resp = self.client.put(
            f'/api/v1/security/sensitive-words/{sw.id}/',
            data=json.dumps({'action': 'block'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 200
        assert resp.json()['action'] == 'block'
        sw.refresh_from_db()
        assert sw.action == 'block'

    @pytest.mark.integration
    def test_update_invalid_action_400(self):
        """PUT 传入非法 action 返回 400"""
        sw = SensitiveWord.objects.create(word='校验词', category='other', action='mask')
        resp = self.client.put(
            f'/api/v1/security/sensitive-words/{sw.id}/',
            data=json.dumps({'action': 'bad_action'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_update_not_found_404(self):
        """PUT 不存在的 id 返回 404"""
        resp = self.client.put(
            '/api/v1/security/sensitive-words/99999/',
            data=json.dumps({'action': 'block'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_delete_204(self):
        """超管删除敏感词返回 204"""
        sw = SensitiveWord.objects.create(word='删除词', category='other', action='mask')
        resp = self.client.delete(
            f'/api/v1/security/sensitive-words/{sw.id}/', **self.admin_headers)
        assert resp.status_code == 204
        assert not SensitiveWord.objects.filter(id=sw.id).exists()

    @pytest.mark.integration
    def test_delete_not_found_404(self):
        """DELETE 不存在的 id 返回 404"""
        resp = self.client.delete(
            '/api/v1/security/sensitive-words/99999/', **self.admin_headers)
        assert resp.status_code == 404


# ============================================================================
# IP 白名单 CRUD —— IpWhitelistView / IpWhitelistDetailView
# ============================================================================
class TestIpWhitelistCRUD(SecurityAPITestBase):
    """IP 白名单 CRUD 接口测试"""

    @pytest.mark.integration
    def test_list_admin_200(self):
        """超管可查看白名单列表（仅返回 is_enabled=True 的记录）"""
        IpWhitelist.objects.create(ip_or_cidr='10.0.0.1', description='测试', created_by=self.super_admin)
        resp = self.client.get('/api/v1/security/ip-whitelist/', **self.admin_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] >= 1

    @pytest.mark.integration
    def test_list_anonymous_401(self):
        """匿名用户访问白名单应 401"""
        resp = self.client.get('/api/v1/security/ip-whitelist/', **self.anon_headers)
        assert resp.status_code in [401, 403]

    @pytest.mark.integration
    def test_list_normal_user_403(self):
        """普通用户访问白名单应 403"""
        resp = self.client.get('/api/v1/security/ip-whitelist/', **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_create_201(self):
        """超管添加白名单 IP 返回 201"""
        resp = self.client.post(
            '/api/v1/security/ip-whitelist/',
            data=json.dumps({'ip_or_cidr': '192.168.1.0/24', 'description': '内网段'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 201
        assert resp.json()['ip_or_cidr'] == '192.168.1.0/24'
        assert IpWhitelist.objects.filter(ip_or_cidr='192.168.1.0/24').exists()

    @pytest.mark.integration
    def test_create_empty_ip_400(self):
        """ip_or_cidr 必填，缺失时返回 400"""
        resp = self.client.post(
            '/api/v1/security/ip-whitelist/',
            data=json.dumps({'description': '无IP'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_duplicate_400(self):
        """重复的 ip_or_cidr 返回 400（unique 约束）"""
        IpWhitelist.objects.create(ip_or_cidr='10.0.0.1', description='已存在')
        resp = self.client.post(
            '/api/v1/security/ip-whitelist/',
            data=json.dumps({'ip_or_cidr': '10.0.0.1', 'description': '重复'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_update_200(self):
        """超管修改白名单描述/启用状态返回 200"""
        obj = IpWhitelist.objects.create(ip_or_cidr='10.0.0.2', description='原描述',
                                          created_by=self.super_admin)
        resp = self.client.put(
            f'/api/v1/security/ip-whitelist/{obj.id}/',
            data=json.dumps({'description': '新描述', 'is_enabled': False}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 200
        assert resp.json()['description'] == '新描述'
        assert resp.json()['is_enabled'] is False

    @pytest.mark.integration
    def test_update_not_found_404(self):
        """PUT 不存在的 id 返回 404"""
        resp = self.client.put(
            '/api/v1/security/ip-whitelist/99999/',
            data=json.dumps({'description': 'x'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_delete_204(self):
        """超管删除白名单返回 204"""
        obj = IpWhitelist.objects.create(ip_or_cidr='10.0.0.3', description='待删',
                                          created_by=self.super_admin)
        resp = self.client.delete(
            f'/api/v1/security/ip-whitelist/{obj.id}/', **self.admin_headers)
        assert resp.status_code == 204
        assert not IpWhitelist.objects.filter(id=obj.id).exists()

    @pytest.mark.integration
    def test_delete_not_found_404(self):
        """DELETE 不存在的 id 返回 404"""
        resp = self.client.delete(
            '/api/v1/security/ip-whitelist/99999/', **self.admin_headers)
        assert resp.status_code == 404


# ============================================================================
# IP 黑名单 CRUD —— IpBlacklistView / IpBlacklistDetailView
# ============================================================================
class TestIpBlacklistCRUD(SecurityAPITestBase):
    """IP 黑名单 CRUD 接口测试"""

    @pytest.mark.integration
    def test_list_admin_200(self):
        """超管可查看黑名单列表（仅返回 is_active=True 的记录）"""
        IpBlacklist.objects.create(ip='1.2.3.4', reason='manual', detail='测试')
        resp = self.client.get('/api/v1/security/ip-blacklist/', **self.admin_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] >= 1

    @pytest.mark.integration
    def test_list_anonymous_401(self):
        """匿名用户访问黑名单应 401"""
        resp = self.client.get('/api/v1/security/ip-blacklist/', **self.anon_headers)
        assert resp.status_code in [401, 403]

    @pytest.mark.integration
    def test_list_normal_user_403(self):
        """普通用户访问黑名单应 403"""
        resp = self.client.get('/api/v1/security/ip-blacklist/', **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_create_201(self):
        """超管添加黑名单 IP 返回 201（created=True）"""
        resp = self.client.post(
            '/api/v1/security/ip-blacklist/',
            data=json.dumps({'ip': '5.6.7.8', 'reason': 'manual', 'detail': '测试封禁'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 201
        assert resp.json()['ip'] == '5.6.7.8'
        assert resp.json()['created'] is True
        assert IpBlacklist.objects.filter(ip='5.6.7.8').exists()

    @pytest.mark.integration
    def test_create_empty_ip_400(self):
        """ip 必填，缺失时返回 400"""
        resp = self.client.post(
            '/api/v1/security/ip-blacklist/',
            data=json.dumps({'reason': 'manual'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_update_existing_ip(self):
        """已存在的 IP 再次 POST 走 update_or_create，返回 200（created=False）

        同一 IP 不会创建多条记录，而是更新 reason/detail 并重新激活。
        """
        IpBlacklist.objects.create(ip='9.10.11.12', reason='login_fail', detail='旧原因')
        resp = self.client.post(
            '/api/v1/security/ip-blacklist/',
            data=json.dumps({'ip': '9.10.11.12', 'reason': 'manual', 'detail': '新原因'}),
            content_type='application/json',
            **self.admin_headers
        )
        assert resp.status_code == 200
        assert resp.json()['created'] is False
        # 记录被更新而非新增
        assert IpBlacklist.objects.filter(ip='9.10.11.12').count() == 1
        obj = IpBlacklist.objects.get(ip='9.10.11.12')
        assert obj.reason == 'manual'
        assert obj.detail == '新原因'

    @pytest.mark.integration
    def test_unblock_200(self):
        """PUT 黑名单设置 is_active=False（解封），返回 200"""
        obj = IpBlacklist.objects.create(ip='13.14.15.16', reason='manual', detail='测试')
        resp = self.client.put(
            f'/api/v1/security/ip-blacklist/{obj.id}/', **self.admin_headers)
        assert resp.status_code == 200
        assert resp.json()['is_active'] is False
        obj.refresh_from_db()
        assert obj.is_active is False

    @pytest.mark.integration
    def test_unblock_not_found_404(self):
        """PUT 不存在的黑名单 id 返回 404"""
        resp = self.client.put(
            '/api/v1/security/ip-blacklist/99999/', **self.admin_headers)
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_delete_204(self):
        """超管删除黑名单记录返回 204"""
        obj = IpBlacklist.objects.create(ip='17.18.19.20', reason='manual', detail='待删')
        resp = self.client.delete(
            f'/api/v1/security/ip-blacklist/{obj.id}/', **self.admin_headers)
        assert resp.status_code == 204
        assert not IpBlacklist.objects.filter(id=obj.id).exists()

    @pytest.mark.integration
    def test_delete_not_found_404(self):
        """DELETE 不存在的黑名单 id 返回 404"""
        resp = self.client.delete(
            '/api/v1/security/ip-blacklist/99999/', **self.admin_headers)
        assert resp.status_code == 404


# ============================================================================
# 登录尝试列表 —— LoginAttemptView
# ============================================================================
class TestLoginAttemptList(SecurityAPITestBase):
    """登录尝试日志列表 + 多维度过滤 + 分页"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """在基类环境基础上预置登录记录"""
        self._init_env()
        # 预置多条登录记录，供过滤与分页测试
        LoginAttempt.objects.create(username='alice', ip='10.0.0.1', result='success')
        LoginAttempt.objects.create(username='alice', ip='10.0.0.2', result='wrong_password')
        LoginAttempt.objects.create(username='bob', ip='10.0.0.1', result='locked')

    @pytest.mark.integration
    def test_list_admin_200(self):
        """超管可查看登录尝试列表，返回分页结构"""
        resp = self.client.get('/api/v1/security/login-attempts/', **self.admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert 'total' in data
        assert 'rows' in data
        assert data['total'] >= 3

    @pytest.mark.integration
    def test_list_anonymous_401(self):
        """匿名用户访问登录尝试应 401"""
        resp = self.client.get('/api/v1/security/login-attempts/', **self.anon_headers)
        assert resp.status_code in [401, 403]

    @pytest.mark.integration
    def test_list_normal_user_403(self):
        """普通用户访问登录尝试应 403"""
        resp = self.client.get('/api/v1/security/login-attempts/', **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_filter_by_result(self):
        """按 result 过滤：只返回匹配结果类型的记录"""
        resp = self.client.get(
            '/api/v1/security/login-attempts/?result=success', **self.admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        # 全部记录的 result 均为 success
        assert all(r['result'] == 'success' for r in data['rows'])
        assert data['total'] >= 1

    @pytest.mark.integration
    def test_filter_by_username(self):
        """按 username 模糊过滤：icase 包含匹配"""
        resp = self.client.get(
            '/api/v1/security/login-attempts/?username=alice', **self.admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert all(r['username'] == 'alice' for r in data['rows'])
        assert data['total'] >= 2

    @pytest.mark.integration
    def test_filter_by_ip(self):
        """按 ip 模糊过滤：icase 包含匹配"""
        resp = self.client.get(
            '/api/v1/security/login-attempts/?ip=10.0.0.1', **self.admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert all('10.0.0.1' in r['ip'] for r in data['rows'])
        assert data['total'] >= 2

    @pytest.mark.integration
    def test_pagination(self):
        """分页参数：page_size 控制每页条数"""
        resp = self.client.get(
            '/api/v1/security/login-attempts/?page=1&page_size=2', **self.admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['page'] == 1
        assert data['page_size'] == 2
        assert len(data['rows']) <= 2
        assert data['total'] >= 3


class TestSensitiveWordExtraBranches(SecurityAPITestBase):
    """敏感词创建竞态与过滤器重载异常路径"""

    @pytest.mark.integration
    def test_create_integrity_error_400(self):
        """exists() 检查与 create 之间的并发竞态由 IntegrityError 兜底为 400"""
        with patch.object(SensitiveWord.objects, 'create',
                          side_effect=__import__('django.db', fromlist=['IntegrityError']).IntegrityError()), \
             patch.object(security_views.SensitiveWordView, '_trigger_reload'):
            resp = self.client.post(
                '/api/v1/security/sensitive-words/',
                data=json.dumps({'word': '竞态词', 'category': 'other', 'action': 'mask'}),
                content_type='application/json',
                **self.admin_headers)
        assert resp.status_code == 400
        assert '已存在' in resp.json()['detail']

    @pytest.mark.integration
    def test_trigger_reload_success_and_failure(self):
        """force_reload 成功路径不抛异常；异常时被吞掉不影响接口"""
        from apps.security.sensitive_filter import SensitiveFilter
        with patch.object(SensitiveFilter, 'force_reload') as mock_reload:
            security_views.SensitiveWordView._trigger_reload()
            mock_reload.assert_called_once()
        with patch.object(SensitiveFilter, 'force_reload', side_effect=RuntimeError('reload fail')):
            security_views.SensitiveWordView._trigger_reload()  # 不应抛异常

    @pytest.mark.integration
    def test_post_with_real_reload(self):
        """未 mock 时创建敏感词会真实触发 force_reload（异常不影响接口）"""
        resp = self.client.post(
            '/api/v1/security/sensitive-words/',
            data=json.dumps({'word': '真实词', 'category': 'secret', 'action': 'block'}),
            content_type='application/json',
            **self.admin_headers)
        assert resp.status_code == 201


class TestMiscBranches(SecurityAPITestBase):
    """白名单 creator 显示名回退与登录尝试分页补充"""

    @pytest.mark.integration
    def test_whitelist_list_creator_fallback_to_username(self):
        """created_by.real_name 为空时回退到 username"""
        # 第一个记录 real_name 为空 -> 回退 username；第二个有 real_name
        user_no_real_name = _create_test_user('no_realname', is_super_admin=False)
        IpWhitelist.objects.create(ip_or_cidr='10.1.1.1', created_by=user_no_real_name)
        IpWhitelist.objects.create(
            ip_or_cidr='10.1.1.2', created_by=self.super_admin)  # super_admin 有 real_name
        resp = self.client.get('/api/v1/security/ip-whitelist/', **self.admin_headers)
        assert resp.status_code == 200
        rows = resp.json()['rows']
        by_ip = {r['ip_or_cidr']: r for r in rows}
        assert by_ip['10.1.1.1']['creator'] == 'no_realname'
        assert by_ip['10.1.1.2']['creator'] == '管理员'

    @pytest.mark.integration
    def test_login_attempts_page_beyond_range(self):
        """页码超出范围返回空 rows 但保留 total"""
        LoginAttempt.objects.create(username='alice', ip='10.0.0.1', result='success')
        resp = self.client.get(
            '/api/v1/security/login-attempts/?page=99&page_size=20',
            **self.admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['page'] == 99
        assert data['rows'] == []
        assert data['total'] >= 1
