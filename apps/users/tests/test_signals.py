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
    User, Role, UserRoleRel, UserDeptScopeRel, UserTeamScopeRel, RolePermissionRel,
    Permission, GrantStatus, Department, Team)
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
        """Redis 可用时 scan 匹配的 key 通过 pipeline 批量删除 + available_depts_list"""
        conn = MagicMock()
        conn.scan_iter.return_value = iter(['allowed_visibility_1', 'allowed_visibility_2'])
        pipe = MagicMock()
        conn.pipeline.return_value = pipe
        # get_redis_connection 在 signals 内部 import（django_redis），patch 原模块路径
        with patch('django_redis.get_redis_connection', return_value=conn) as mock_get:
            signals._invalidate_visibility_cache()
        mock_get.assert_called_once_with('default')
        # pipeline 内逐条 delete 可见性 key
        pipe.delete.assert_any_call('allowed_visibility_1')
        pipe.delete.assert_any_call('allowed_visibility_2')
        pipe.execute.assert_called_once()
        # available_depts_list 走 conn 直接删除（非 pipeline）
        conn.delete.assert_called_once_with('available_depts_list')

    @pytest.mark.unit
    def test_django_redis_unavailable_silent(self):
        """django_redis 不可用时静默跳过，不抛异常"""
        with patch.dict('sys.modules', {'django_redis': None}):
            signals._invalidate_visibility_cache()  # 不应抛异常

    @pytest.mark.unit
    def test_redis_error_logs_and_silently_fails(self):
        """Redis 连接异常时仅记日志，不抛异常"""
        with patch('django_redis.get_redis_connection',
                   side_effect=RuntimeError('redis down')):
            signals._invalidate_visibility_cache()  # 不应抛异常


class TestDelayedInvalidateCache:
    """_delayed_invalidate_cache 延迟双删调度测试

    eager 模式（测试环境 CELERY_TASK_ALWAYS_EAGER=True）下同步执行任务体；
    生产环境通过 Celery send_task 延迟派发。
    """

    @pytest.mark.unit
    def test_send_task_with_countdown_when_not_eager_then_calls_send_task(self):
        """非 eager 模式应通过 Celery 发送延迟任务，countdown=5"""
        mock_app = MagicMock()
        # current_app 在 signals 内部 import（celery），patch 原模块路径
        with patch('celery.current_app', mock_app), \
                patch('django.conf.settings.CELERY_TASK_ALWAYS_EAGER', False):
            signals._delayed_invalidate_cache()
        mock_app.send_task.assert_called_once_with(
            'apps.users.tasks.delayed_invalidate_visibility_cache', countdown=5)

    @pytest.mark.unit
    def test_delayed_invalidate_when_eager_then_calls_invalidate_synchronously(self):
        """eager 模式下不派发 Celery 任务，同步执行缓存失效"""
        with patch('apps.users.signals._invalidate_visibility_cache') as mock_inv:
            signals._delayed_invalidate_cache()
        mock_inv.assert_called_once()


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


class TestVisibilityCacheErrorBranches:
    """_invalidate_visibility_cache 兜底异常分支测试"""

    @pytest.mark.unit
    def test_delete_pattern_error_logged(self):
        """Redis 异常 → 记 debug 日志不抛出（非 Redis 后端统一降级）"""
        with patch('django_redis.get_redis_connection',
                   side_effect=RuntimeError('redis down')), \
                patch('apps.users.signals.logger') as mock_logger:
            signals._invalidate_visibility_cache()
        mock_logger.debug.assert_called_once()


class TestDelayedInvalidateThreadFallback:
    """_delayed_invalidate_cache Celery 不可用时回退线程测试"""

    @pytest.mark.unit
    def test_celery_unavailable_uses_thread(self):
        """Celery ImportError → 创建守护线程延迟删除"""
        with patch('django.conf.settings.CELERY_TASK_ALWAYS_EAGER', False), \
                patch.dict('sys.modules', {'celery': None}), \
                patch('apps.users.signals._invalidate_visibility_cache') as mock_inv, \
                patch('threading.Thread') as mock_thread:
            signals._delayed_invalidate_cache(delay=0)
            # 线程为异步执行，手动调用 target 验证内部逻辑
            target = mock_thread.call_args.kwargs['target']
            target()
        mock_inv.assert_called_once()


class TestDeptTeamDeleteSignals:
    """部门/团队删除 → 缓存失效信号测试"""

    @pytest.mark.django_db
    def test_department_delete_invalidates_cache(self):
        """部门删除应清理可见性缓存并延迟清理"""
        dept = Department.objects.create(name='临时部')
        with patch('apps.users.signals._invalidate_visibility_cache') as mock_inv, \
                patch('apps.users.signals._delayed_invalidate_cache') as mock_delayed:
            dept.delete()
        mock_inv.assert_called_once()
        mock_delayed.assert_called_once()

    @pytest.mark.django_db
    def test_team_delete_invalidates_cache(self):
        """团队删除应清理可见性缓存并延迟清理"""
        dept = Department.objects.create(name='父部')
        team = Team.objects.create(name='子组', department=dept)
        with patch('apps.users.signals._invalidate_visibility_cache') as mock_inv, \
                patch('apps.users.signals._delayed_invalidate_cache') as mock_delayed:
            team.delete()
        mock_inv.assert_called_once()
        mock_delayed.assert_called_once()


class TestNodeSyncSignals:
    """部门/团队保存 → 知识节点树同步信号测试"""

    @pytest.mark.django_db
    def test_department_save_raw_skips_sync(self):
        """raw=True（fixture 加载）→ 跳过节点同步"""
        dept = Department.objects.create(name='raw部')
        with patch('apps.knowledge.node_sync.sync_dept_node') as mock_sync:
            signals.on_department_node_sync(sender=Department, instance=dept, raw=True)
        mock_sync.assert_not_called()

    @pytest.mark.django_db
    def test_department_save_syncs_node(self):
        """部门保存应同步知识节点树"""
        with patch('apps.knowledge.node_sync.sync_dept_node') as mock_sync:
            Department.objects.create(name='sync部')
        mock_sync.assert_called_once()

    @pytest.mark.django_db
    def test_department_sync_error_logged(self):
        """节点同步异常 → 记录 error 不阻断"""
        dept = Department.objects.create(name='err部')
        with patch('apps.knowledge.node_sync.sync_dept_node',
                   side_effect=Exception('db down')), \
                patch('apps.users.signals.logger') as mock_logger:
            signals.on_department_node_sync(sender=Department, instance=dept)
        mock_logger.error.assert_called_once()

    @pytest.mark.django_db
    def test_team_save_raw_skips_sync(self):
        """团队保存 raw=True → 跳过节点同步"""
        dept = Department.objects.create(name='raw部组')
        team = Team.objects.create(name='raw组', department=dept)
        with patch('apps.knowledge.node_sync.sync_team_node') as mock_sync:
            signals.on_team_node_sync(sender=Team, instance=team, raw=True)
        mock_sync.assert_not_called()

    @pytest.mark.django_db
    def test_team_save_syncs_node(self):
        """团队保存应同步知识节点树"""
        with patch('apps.knowledge.node_sync.sync_team_node') as mock_sync:
            dept = Department.objects.create(name='sync部组')
            Team.objects.create(name='sync组', department=dept)
        mock_sync.assert_called_once()

    @pytest.mark.django_db
    def test_team_sync_error_logged(self):
        """团队节点同步异常 → 记录 error 不阻断"""
        dept = Department.objects.create(name='err部组')
        team = Team.objects.create(name='err组', department=dept)
        with patch('apps.knowledge.node_sync.sync_team_node',
                   side_effect=Exception('db down')), \
                patch('apps.users.signals.logger') as mock_logger:
            signals.on_team_node_sync(sender=Team, instance=team)
        mock_logger.error.assert_called_once()


class TestUserMemorySignalsEdge:
    """用户记忆初始化/清理的异常分支测试"""

    @pytest.mark.django_db
    def test_user_create_raw_skips_init_memory(self):
        """raw=True（fixture 加载）→ 跳过 UserMemory 初始化"""
        user = _make_user(username='raw-mem')
        with patch('apps.memory.models.UserMemory.objects.get_or_create') as mock_get:
            signals.on_user_create_init_memory(
                sender=User, instance=user, created=True, raw=True)
        mock_get.assert_not_called()

    @pytest.mark.django_db
    def test_user_create_memory_init_error_logged(self):
        """UserMemory 初始化异常 → 记录 error 不阻断"""
        user = _make_user(username='err-mem')
        with patch('apps.memory.models.UserMemory.objects.get_or_create',
                   side_effect=Exception('db down')), \
                patch('apps.users.signals.logger') as mock_logger:
            signals.on_user_create_init_memory(sender=User, instance=user, created=True)
        mock_logger.error.assert_called_once()

    @pytest.mark.django_db
    def test_user_update_skips_memory_init(self):
        """用户更新（非创建）→ 跳过 UserMemory 初始化"""
        user = _make_user(username='upd-mem')
        with patch('apps.memory.models.UserMemory.objects.get_or_create') as mock_get:
            signals.on_user_create_init_memory(sender=User, instance=user, created=False)
        mock_get.assert_not_called()

    @pytest.mark.django_db
    def test_user_delete_session_memory_cleanup_error_logged(self):
        """清理单条会话记忆异常 → 记录 error 后继续"""
        user = _make_user(username='cleanup-err')
        Session.objects.create(user=user, title='s')
        with patch('apps.memory.short_term.ShortTermMemory') as mock_st, \
                patch('apps.users.signals.logger') as mock_logger:
            mock_st.return_value.clear.side_effect = Exception('cache down')
            signals.on_user_delete_clean_memory(sender=User, instance=user)
        mock_logger.error.assert_called_once()

    @pytest.mark.django_db
    def test_user_delete_memory_outer_error_logged(self):
        """用户记忆清理整体异常 → 记录 error 不抛出"""
        user = _make_user(username='outer-err')
        with patch('apps.memory.models.UserMemory.objects.filter',
                   side_effect=Exception('db down')), \
                patch('apps.users.signals.logger') as mock_logger:
            signals.on_user_delete_clean_memory(sender=User, instance=user)
        mock_logger.error.assert_called_once()


class TestSafeInvalidateErrors:
    """_safe_invalidate_user / _safe_invalidate_role 异常兜底测试"""

    @pytest.mark.unit
    def test_safe_invalidate_user_error_logged(self):
        """失效用户缓存异常 → 记录 error 不抛出"""
        with patch('apps.users.perm_cache.invalidate_user_perms',
                   side_effect=Exception('cache down')), \
                patch('apps.users.signals.logger') as mock_logger:
            signals._safe_invalidate_user(42)
        mock_logger.error.assert_called_once()

    @pytest.mark.unit
    def test_safe_invalidate_role_error_logged(self):
        """失效角色缓存异常 → 记录 error 不抛出"""
        with patch('apps.users.perm_cache.invalidate_role_perms',
                   side_effect=Exception('cache down')), \
                patch('apps.users.signals.logger') as mock_logger:
            signals._safe_invalidate_role(7)
        mock_logger.error.assert_called_once()


class TestScopeRelCacheInvalidation:
    """属地/角色权限绑定变更 → 权限缓存失效信号测试"""

    @pytest.mark.django_db
    def test_dept_scope_rel_save_invalidates_user(self):
        """UserDeptScopeRel 创建 → 失效该用户权限缓存"""
        user = _make_user(username='dept-scope')
        role = _make_role()
        dept = Department.objects.create(name='D')
        with patch('apps.users.perm_cache.invalidate_user_perms') as mock_inv:
            UserDeptScopeRel.objects.create(
                user=user, role=role, dept=dept, status=GrantStatus.ACTIVE)
        mock_inv.assert_called_once_with(user.id)

    @pytest.mark.django_db
    def test_dept_scope_rel_delete_invalidates_user(self):
        """UserDeptScopeRel 删除 → 失效该用户权限缓存"""
        user = _make_user(username='dept-scope2')
        role = _make_role()
        dept = Department.objects.create(name='D2')
        rel = UserDeptScopeRel.objects.create(
            user=user, role=role, dept=dept, status=GrantStatus.ACTIVE)
        with patch('apps.users.perm_cache.invalidate_user_perms') as mock_inv:
            rel.delete()
        mock_inv.assert_called_once_with(user.id)

    @pytest.mark.django_db
    def test_team_scope_rel_save_invalidates_user(self):
        """UserTeamScopeRel 创建 → 失效该用户权限缓存"""
        user = _make_user(username='team-scope')
        role = _make_role()
        dept = Department.objects.create(name='D3')
        team = Team.objects.create(name='T', department=dept)
        with patch('apps.users.perm_cache.invalidate_user_perms') as mock_inv:
            UserTeamScopeRel.objects.create(
                user=user, role=role, team=team, status=GrantStatus.ACTIVE)
        mock_inv.assert_called_once_with(user.id)

    @pytest.mark.django_db
    def test_team_scope_rel_delete_invalidates_user(self):
        """UserTeamScopeRel 删除 → 失效该用户权限缓存"""
        user = _make_user(username='team-scope2')
        role = _make_role()
        dept = Department.objects.create(name='D4')
        team = Team.objects.create(name='T2', department=dept)
        rel = UserTeamScopeRel.objects.create(
            user=user, role=role, team=team, status=GrantStatus.ACTIVE)
        with patch('apps.users.perm_cache.invalidate_user_perms') as mock_inv:
            rel.delete()
        mock_inv.assert_called_once_with(user.id)

    @pytest.mark.django_db
    def test_role_permission_rel_save_invalidates_role(self):
        """RolePermissionRel 创建 → 失效所有持有该角色的用户缓存"""
        role = _make_role()
        perm = Permission.objects.create(
            permission_key='test.module.action', permission_name='测试权限',
            module='test', is_builtin=False)
        with patch('apps.users.perm_cache.invalidate_role_perms') as mock_inv:
            RolePermissionRel.objects.create(role=role, permission=perm)
        mock_inv.assert_called_once_with(role.id)

    @pytest.mark.django_db
    def test_role_permission_rel_delete_invalidates_role(self):
        """RolePermissionRel 删除 → 失效所有持有该角色的用户缓存"""
        role = _make_role()
        perm = Permission.objects.create(
            permission_key='test.module.action2', permission_name='测试权限2',
            module='test', is_builtin=False)
        rel = RolePermissionRel.objects.create(role=role, permission=perm)
        with patch('apps.users.perm_cache.invalidate_role_perms') as mock_inv:
            rel.delete()
        mock_inv.assert_called_once_with(role.id)

    @pytest.mark.unit
    def test_dept_scope_rel_save_raw_skips_invalidation(self):
        """属地授权保存 raw=True → 跳过缓存失效"""
        with patch('apps.users.signals._safe_invalidate_user') as mock_safe:
            signals.on_user_dept_scope_rel_changed(
                sender=UserDeptScopeRel, instance=object(), raw=True)
        mock_safe.assert_not_called()

    @pytest.mark.unit
    def test_team_scope_rel_save_raw_skips_invalidation(self):
        """团队属地授权保存 raw=True → 跳过缓存失效"""
        with patch('apps.users.signals._safe_invalidate_user') as mock_safe:
            signals.on_user_team_scope_rel_changed(
                sender=UserTeamScopeRel, instance=object(), raw=True)
        mock_safe.assert_not_called()

    @pytest.mark.unit
    def test_role_permission_rel_save_raw_skips_invalidation(self):
        """角色权限绑定保存 raw=True → 跳过缓存失效"""
        with patch('apps.users.signals._safe_invalidate_role') as mock_safe:
            signals.on_role_permission_rel_changed(
                sender=RolePermissionRel, instance=object(), raw=True)
        mock_safe.assert_not_called()

    @pytest.mark.unit
    def test_user_role_rel_save_raw_skips_invalidation(self):
        """全局角色授权保存 raw=True → 跳过缓存失效"""
        with patch('apps.users.signals._safe_invalidate_user') as mock_safe:
            signals.on_user_role_rel_changed(
                sender=UserRoleRel, instance=object(), raw=True)
        mock_safe.assert_not_called()

    @pytest.mark.django_db
    def test_user_org_change_invalidates_perms(self):
        """用户调岗（部门/团队变化）→ 全量失效该用户权限缓存"""
        user = _make_user(username='org-change')
        with patch('apps.users.perm_cache.invalidate_user_perms') as mock_inv:
            signals.on_user_org_changed(sender=User, instance=user)
        mock_inv.assert_called_once_with(user.id)
