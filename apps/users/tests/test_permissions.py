"""
apps.users.permissions 测试 —— DRF RBAC 权限类

覆盖范围：
- RequirePerm：未认证拒绝 / 超管快路径 / view.required_perm 优先 / 类属性 perm_key / 未配置默认拒绝
- perm_class：工厂动态生成权限类
- IsSuperAdmin / CanManageUsers / CanReadAudit / RequireKnowledgePerm /
  IsAdminOrOps / CanViewAnalytics：各权限点判定与默认值

采用 mock：
权限类只读 request.user 与 has_permission()，无 DB 依赖，
patch apps.users.permissions.has_permission 即可纯逻辑验证各分支。
"""
import pytest
from unittest.mock import MagicMock, patch

from apps.users.permissions import (
    RequirePerm,
    perm_class,
    IsSuperAdmin,
    CanManageUsers,
    CanReadAudit,
    RequireKnowledgePerm,
    IsAdminOrOps,
    CanViewAnalytics,
)


def _req(user=None, authenticated=True, super_admin=False):
    """构造 mock request.user"""
    u = user or MagicMock()
    u.is_authenticated = authenticated
    u.is_super_admin = super_admin
    req = MagicMock()
    req.user = u
    return req


def _view(required_perm=None):
    """构造带/不带 required_perm 的 mock view"""
    view = MagicMock()
    if required_perm is not None:
        view.required_perm = required_perm
    return view


# ============================================================================
# RequirePerm 基类
# ============================================================================
@pytest.mark.unit
class TestRequirePerm:
    """RequirePerm 判定链路测试"""

    def test_unauthenticated_denied(self):
        """未认证用户直接拒绝，不查询权限"""
        perm = RequirePerm()
        with patch('apps.users.permissions.has_permission') as mock_hp:
            assert perm.has_permission(_req(authenticated=False), _view()) is False
        mock_hp.assert_not_called()

    def test_super_admin_fast_path(self):
        """super_admin 直接放行，不查询权限点"""
        perm = RequirePerm()
        with patch('apps.users.permissions.has_permission') as mock_hp:
            assert perm.has_permission(_req(super_admin=True), _view()) is True
        mock_hp.assert_not_called()

    def test_view_required_perm_priority(self):
        """view.required_perm 优先于类属性 perm_key"""
        perm = RequirePerm()
        perm.perm_key = 'kb.document.read'
        with patch('apps.users.permissions.has_permission', return_value=True) as mock_hp:
            assert perm.has_permission(_req(), _view(required_perm='kb.document.upload')) is True
        mock_hp.assert_called_once()
        assert mock_hp.call_args[0][1] == 'kb.document.upload'

    def test_class_perm_key_fallback(self):
        """无 view.required_perm 时回退类属性 perm_key"""
        perm = RequirePerm()
        perm.perm_key = 'kb.document.read'
        with patch('apps.users.permissions.has_permission', return_value=False) as mock_hp:
            assert perm.has_permission(_req(), _view()) is False
        assert mock_hp.call_args[0][1] == 'kb.document.read'

    def test_no_key_default_deny(self):
        """未配置任何权限点时应默认拒绝（最小权限铁律）"""
        perm = RequirePerm()
        perm.perm_key = ''
        with patch('apps.users.permissions.has_permission') as mock_hp:
            assert perm.has_permission(_req(), _view()) is False
        mock_hp.assert_not_called()


# ============================================================================
# perm_class 工厂
# ============================================================================
@pytest.mark.unit
class TestPermClass:
    """perm_class 工厂测试"""

    def test_factory_creates_class_with_key(self):
        """工厂生成的类应携带对应 perm_key 且走 RequirePerm 逻辑"""
        cls = perm_class('kb.document.upload')
        assert issubclass(cls, RequirePerm)
        assert cls.perm_key == 'kb.document.upload'
        with patch('apps.users.permissions.has_permission', return_value=True) as mock_hp:
            assert cls().has_permission(_req(), _view()) is True
        assert mock_hp.call_args[0][1] == 'kb.document.upload'


# ============================================================================
# 各专用权限类
# ============================================================================
@pytest.mark.unit
class TestIsSuperAdmin:
    """IsSuperAdmin 仅超管放行"""

    def test_super_admin_allowed(self):
        assert IsSuperAdmin().has_permission(_req(super_admin=True), _view()) is True

    def test_normal_user_denied(self):
        assert IsSuperAdmin().has_permission(_req(), _view()) is False

    def test_unauthenticated_denied(self):
        assert IsSuperAdmin().has_permission(_req(authenticated=False), _view()) is False


@pytest.mark.unit
class TestCanManageUsers:
    """CanManageUsers user.manage_all / user.manage"""

    def test_super_admin_allowed(self):
        assert CanManageUsers().has_permission(_req(super_admin=True), _view()) is True

    def test_manage_all_allowed(self):
        with patch('apps.users.permissions.has_permission', return_value=True):
            assert CanManageUsers().has_permission(_req(), _view()) is True

    def test_both_denied(self):
        with patch('apps.users.permissions.has_permission', return_value=False):
            assert CanManageUsers().has_permission(_req(), _view()) is False

    def test_queries_both_keys(self):
        """应同时检查 user.manage_all 与 user.manage"""
        with patch('apps.users.permissions.has_permission',
                   side_effect=[False, True]) as mock_hp:
            assert CanManageUsers().has_permission(_req(), _view()) is True
        assert mock_hp.call_args_list[0][0][1] == 'user.manage_all'
        assert mock_hp.call_args_list[1][0][1] == 'user.manage'


@pytest.mark.unit
class TestCanReadAudit:
    """CanReadAudit audit.log.read"""

    def test_super_admin_allowed(self):
        assert CanReadAudit().has_permission(_req(super_admin=True), _view()) is True

    def test_permission_allowed(self):
        with patch('apps.users.permissions.has_permission', return_value=True):
            assert CanReadAudit().has_permission(_req(), _view()) is True

    def test_permission_denied(self):
        with patch('apps.users.permissions.has_permission', return_value=False):
            assert CanReadAudit().has_permission(_req(), _view()) is False


@pytest.mark.unit
class TestRequireKnowledgePerm:
    """RequireKnowledgePerm 默认 kb.document.read"""

    def test_default_perm_key(self):
        """无 required_perm 时默认 kb.document.read"""
        with patch('apps.users.permissions.has_permission', return_value=True) as mock_hp:
            assert RequireKnowledgePerm().has_permission(_req(), _view()) is True
        assert mock_hp.call_args[0][1] == 'kb.document.read'

    def test_view_required_perm(self):
        with patch('apps.users.permissions.has_permission', return_value=True) as mock_hp:
            assert RequireKnowledgePerm().has_permission(
                _req(), _view(required_perm='kb.document.delete')) is True
        assert mock_hp.call_args[0][1] == 'kb.document.delete'

    def test_super_admin_allowed(self):
        assert RequireKnowledgePerm().has_permission(_req(super_admin=True), _view()) is True


@pytest.mark.unit
class TestIsAdminOrOps:
    """IsAdminOrOps kb.node.manage / kb.manage_all"""

    def test_node_manage_allowed(self):
        with patch('apps.users.permissions.has_permission',
                   side_effect=[True, False]) as mock_hp:
            assert IsAdminOrOps().has_permission(_req(), _view()) is True
        assert mock_hp.call_args_list[0][0][1] == 'kb.node.manage'

    def test_manage_all_allowed(self):
        with patch('apps.users.permissions.has_permission',
                   side_effect=[False, True]):
            assert IsAdminOrOps().has_permission(_req(), _view()) is True

    def test_both_denied(self):
        with patch('apps.users.permissions.has_permission', return_value=False):
            assert IsAdminOrOps().has_permission(_req(), _view()) is False


@pytest.mark.unit
class TestCanViewAnalytics:
    """CanViewAnalytics 默认 analytics.org.read"""

    def test_default_perm_key(self):
        with patch('apps.users.permissions.has_permission', return_value=True) as mock_hp:
            assert CanViewAnalytics().has_permission(_req(), _view()) is True
        assert mock_hp.call_args[0][1] == 'analytics.org.read'

    def test_view_required_perm(self):
        with patch('apps.users.permissions.has_permission', return_value=True) as mock_hp:
            assert CanViewAnalytics().has_permission(
                _req(), _view(required_perm='analytics.system.read')) is True
        assert mock_hp.call_args[0][1] == 'analytics.system.read'

    def test_super_admin_allowed(self):
        assert CanViewAnalytics().has_permission(_req(super_admin=True), _view()) is True
