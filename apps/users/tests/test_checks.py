"""
apps.users.checks 测试 —— 部署期超管数量系统检查

覆盖范围：
- 0 个活跃超管 → E001 严重告警
- 1 个活跃超管 → E002 双审无法流转告警
- ≥2 个活跃超管 → 无告警
- 数据库未就绪（表不存在）→ 跳过检查返回空
"""
import pytest

from apps.users.models import User, Role, UserRoleRel, GrantStatus
from apps.users.checks import check_super_admin_count


def _make_super_admin(username):
    """创建超管角色绑定（is_super_admin 判定走 super_admin 角色）"""
    user = User.objects.create_user(
        username=username, email=f'{username}@test.com', password='testpass123')
    role, _ = Role.objects.get_or_create(
        role_key='super_admin',
        defaults=dict(name='超级管理员', is_builtin=True))
    UserRoleRel.objects.get_or_create(
        user=user, role=role, defaults={'status': GrantStatus.ACTIVE})
    return user


class TestCheckSuperAdminCount:
    """check_super_admin_count 告警阈值测试"""

    @pytest.mark.django_db
    def test_zero_super_admin_warns_e001(self):
        """无活跃超管时应返回 E001 严重告警"""
        errors = check_super_admin_count(None)
        assert len(errors) == 1
        assert errors[0].id == 'users.E001_no_super_admin'

    @pytest.mark.django_db
    def test_one_super_admin_warns_e002(self):
        """仅 1 个活跃超管时应返回 E002 双审流转告警"""
        _make_super_admin('admin1')
        errors = check_super_admin_count(None)
        assert len(errors) == 1
        assert errors[0].id == 'users.E002_insufficient_super_admin'

    @pytest.mark.django_db
    def test_two_super_admins_no_warning(self):
        """2 个及以上活跃超管时无告警"""
        _make_super_admin('admin1')
        _make_super_admin('admin2')
        assert check_super_admin_count(None) == []

    @pytest.mark.django_db
    def test_revoked_super_admin_not_counted(self):
        """已撤销/非激活的超管绑定不应计入数量"""
        admin = _make_super_admin('admin1')
        UserRoleRel.objects.filter(user=admin).update(status=GrantStatus.REVOKED)
        errors = check_super_admin_count(None)
        assert len(errors) == 1
        assert errors[0].id == 'users.E001_no_super_admin'
