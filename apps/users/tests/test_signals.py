"""
apps.users.signals 测试 —— 缓存失效 + 记忆初始化 + 权限缓存失效信号

覆盖范围：
- _invalidate_visibility_cache：Redis scan+delete、django_redis 不可用、
  delete_pattern 兜底
- _delayed_invalidate_cache：通过 Celery send_task 延迟双删
- on_user_create_init_memory：新用户自动初始化 UserMemory 并写入画像
- on_user_delete_clean_memory：删除用户时清理记忆
- on_user_role_rel_changed：角色授权变更触发用户权限缓存失效
- tasks.delayed_invalidate_visibility_cache：Celery 任务透传调用

部分 mock 部分用 DB：
缓存/调度类逻辑依赖 Redis/Celery 连接，patch 隔离；记忆初始化为 ORM 行为，
需真实 DB 验证画像拼装结果。
"""
from unittest.mock import MagicMock, patch

import pytest

from apps.users.models import (
    User, Role, UserRoleRel, GrantStatus, Department, Team)
from apps.users import signals
from apps.users.tasks import delayed_invalidate_visibility_cache
from apps.memory.models import UserMemory, Session, SessionMemory


def _make_user(username='sig-user', real_name='', department=None, team=None):
    return User.objects.create_user(
        username=username, email=f'{username}@test.com', password='testpass123',
        real_name=real_name, department=department, team=team)


def _make_role(role_key='dept_manager'):
    return Role.objects.get_or_create(
        role_key=role_key, defaults=dict(name=role_key, is_builtin=True))[0]


class TestInvalidateVisibilityCache:
    """_invalidate_visibility_cache 三种后端分支测试"""

    @pytest.mark.unit
    def test_redis_scan_and_delete(self):
        """Redis 可用时 scan 匹配的 key 与 available_depts_list 全部删除"""
        conn = MagicMock()
        conn.scan_iter.return_value = iter(['allowed_visibility_1', 'allowed_visibility_2'])
        with patch('apps.users.signals.get_redis_connection', return_value=conn) as mock_get:
            signals._invalidate_visibility_cache()
        mock_get.assert_called_once_with('default')
        assert conn.delete.call_args_list[0][0] == ('allowed_visibility_1',)
        assert conn.delete.call_args_list[1][0] == ('allowed_visibility_2',)
        assert conn.delete.call_args_list[2][0] == ('available_depts_list',)

    @pytest.mark.unit
    def test_django_redis_unavailable_silent(self):
        """django_redis 不可用时静默跳过，不抛异常"""
        with patch.dict('sys.modules', {'django_redis': None}):
            signals._invalidate_visibility_cache()  # 不应抛异常

    @pytest.mark.unit
    def test_redis_error_falls_back_to_delete_pattern(self):
        """Redis 连接异常时回退 cache.delete_pattern 兜底"""
        with patch('apps.users.signals.get_redis_connection',
                   side_effect=RuntimeError('redis down')), \
                patch('apps.users.signals.cache') as mock_cache:
            signals._invalidate_visibility_cache()
        mock_cache.delete_pattern.assert_called_once_with('allowed_visibility_*')
        mock_cache.delete.assert_called_once_with('available_depts_list')


class TestDelayedInvalidateCache:
    """_delayed_invalidate_cache 延迟双删调度测试"""

    @pytest.mark.unit
    def test_send_task_with_countdown(self):
        """应通过 Celery 发送延迟任务，countdown=5"""
        mock_app = MagicMock()
        with patch('apps.users.signals.current_app', mock_app):
            signals._delayed_invalidate_cache()
        mock_app.send_task.assert_called_once_with(
            'apps.users.tasks.delayed_invalidate_visibility_cache', countdown=5)


@pytest.fixture
def org_env():
    """部门 + 团队环境（用于验证用户画像拼装）"""
    dept = Department.objects.create(name='研发部', sort_order=1)
    team = Team.objects.create(name='后端组', department=dept)
    return {'dept': dept, 'team': team}


class TestUserMemorySignals:
    """用户创建/删除时的记忆信号测试"""

    @pytest.mark.django_db
    def test_user_create_initializes_memory_with_profile(self, org_env):
        """新用户创建应自动初始化 UserMemory，画像含姓名/部门/团队"""
        user = _make_user(username='newbie', real_name='张三',
                          department=org_env['dept'], team=org_env['team'])
        um = UserMemory.objects.get(user=user)
        assert '姓名：张三' in um.profile_text
        assert '部门：研发部' in um.profile_text
        assert '团队：后端组' in um.profile_text

    @pytest.mark.django_db
    def test_user_create_without_org_keeps_empty_profile(self):
        """无姓名/部门/团队的新用户画像保持为空"""
        user = _make_user(username='naked')
        um = UserMemory.objects.get(user=user)
        assert um.profile_text == ''

    @pytest.mark.django_db
    def test_user_delete_cleans_memory(self):
        """删除用户应清理其 UserMemory（及关联会话记忆）"""
        user = _make_user(username='bye-user')
        um = UserMemory.objects.get(user=user)
        sess = Session.objects.create(user=user, title='会话')
        SessionMemory.objects.create(session=sess, summary='摘要')
        assert UserMemory.objects.filter(user=user).exists()

        user.delete()

        assert not UserMemory.objects.filter(user_id=user.id).exists()
        assert not SessionMemory.objects.filter(session_id=sess.id).exists()


class TestRoleRelCacheInvalidation:
    """角色授权变更 → 用户权限缓存失效信号测试"""

    @pytest.mark.django_db
    def test_user_role_rel_save_invalidates_user_perms(self):
        """UserRoleRel 创建应触发该用户 L1 权限缓存失效"""
        user = _make_user(username='role-user')
        role = _make_role()
        with patch('apps.users.perm_cache.invalidate_user_perms') as mock_inv:
            UserRoleRel.objects.create(
                user=user, role=role, status=GrantStatus.ACTIVE)
        mock_inv.assert_called_once_with(user.id)

    @pytest.mark.django_db
    def test_user_role_rel_delete_invalidates_user_perms(self):
        """UserRoleRel 删除同样触发失效"""
        user = _make_user(username='role-user2')
        role = _make_role()
        rel = UserRoleRel.objects.create(user=user, role=role, status=GrantStatus.ACTIVE)
        with patch('apps.users.perm_cache.invalidate_user_perms') as mock_inv:
            rel.delete()
        mock_inv.assert_called_once_with(user.id)


class TestDelayedInvalidateTask:
    """delayed_invalidate_visibility_cache 任务测试"""

    @pytest.mark.unit
    def test_task_calls_invalidate(self):
        """任务应透传调用 _invalidate_visibility_cache"""
        with patch('apps.users.tasks._invalidate_visibility_cache') as mock_inv:
            delayed_invalidate_visibility_cache()
        mock_inv.assert_called_once()
