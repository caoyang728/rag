"""
部门/团队缓存失效 + 节点树同步
使用延迟双删策略：
1. 修改后立即删除缓存
2. 延迟5秒再次删除缓存（防止并发问题）

节点树同步信号使用 thread-local 标记防止递归触发（sync_*_node 内部
可能保存同一实例，导致 post_save 再次触发本信号）。
"""
import threading

from loguru import logger
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.core.cache import cache

from .models import (
    Department, Team, User, Role,
    UserRoleRel, UserDeptScopeRel, UserTeamScopeRel,
    RolePermissionRel,
)

# thread-local 标记：防止节点同步信号递归触发
_sync_guard = threading.local()


def _invalidate_visibility_cache():
    """删除所有 allowed_visibility 相关缓存和部门列表缓存

    优先用 Redis scan_iter + pipeline 批量删；django_redis 不可用时静默跳过；
    非 Redis 后端（LocMem 等不支持 scan）也跳过（开发环境重启即清，可接受）。
    """
    try:
        from django_redis import get_redis_connection
        conn = get_redis_connection('default')
        keys = list(conn.scan_iter('allowed_visibility_*'))
        if keys:
            pipe = conn.pipeline(transaction=False)
            for key in keys:
                pipe.delete(key)
            pipe.execute()
        conn.delete('available_depts_list')
        logger.debug(f"Invalidated {len(keys)} visibility cache keys and available_depts_list")
    except ImportError:
        logger.debug("django_redis not available, skipping cache invalidation")
    except Exception as e:
        # 非 Redis 后端 / Redis 不可用 / scan 不支持等统一降级，不阻断主业务
        logger.debug(f"Visibility cache invalidation skipped: {e}")


def _delayed_invalidate_cache(delay=5):
    """延迟删除缓存（延迟双删）"""
    try:
        # 测试/调试环境（CELERY_TASK_ALWAYS_EAGER=True）下 send_task 不会真正派发任务，
        # Celery 会抛 AlwaysEagerIgnored 告警，这里直接同步执行任务体，保证缓存失效真实生效
        from django.conf import settings
        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            _invalidate_visibility_cache()
            logger.debug("Invalidated visibility cache synchronously (eager mode)")
            return
        # 生产环境使用 Celery 延迟双删，避免修改后并发读命中旧缓存
        from celery import current_app
        current_app.send_task('apps.users.tasks.delayed_invalidate_visibility_cache', countdown=delay)
        logger.debug("Scheduled delayed cache invalidation via Celery")
    except ImportError:
        # Celery不可用，使用线程（WSGI/ASGI环境下可能不可靠）
        import time
        import threading
        
        def _delayed_delete():
            time.sleep(delay)
            _invalidate_visibility_cache()
        
        t = threading.Thread(target=_delayed_delete, daemon=True)
        t.start()
        logger.debug("Scheduled delayed cache invalidation via thread")


def _invalidate_all_scope():
    """全量失效 L2/L3/L4 权限范围缓存（团队增删改 / 角色 data_scope 变化时触发）

    背景：L2/L3/L4 缓存（perm:scope:*）由 models.get_user_managed_depts / teams /
    data_scope_level 在命中时回填。Team 增删改影响"授权部门下的活跃团队"集合（L3），
    Role.data_scope 变化影响持有者最高数据范围等级（L4），二者都无法精确定位受影响
    用户，故全量保守过失效（权限安全优先），下次请求回源重算。
    与知识可见性缓存（allowed_visibility_*）的失效并行，互不替代。
    """
    try:
        from apps.users.perm_cache import invalidate_all_scope
        invalidate_all_scope()
    except Exception as e:
        # 缓存失效失败不得阻断主业务，仅记日志（权限安全优先但允许降级）
        logger.debug(f"Scope cache invalidation skipped: {e}")


@receiver(post_save, sender=Department)
def department_post_save(sender, instance, created, **kwargs):
    """部门保存后清除可见性缓存（延迟双删）"""
    _invalidate_visibility_cache()
    _delayed_invalidate_cache()


@receiver(post_delete, sender=Department)
def department_post_delete(sender, instance, **kwargs):
    """部门删除后清除可见性缓存（延迟双删）"""
    _invalidate_visibility_cache()
    _delayed_invalidate_cache()


@receiver(post_save, sender=Team)
def team_post_save(sender, instance, created, **kwargs):
    """团队保存后清除可见性缓存（延迟双删）"""
    _invalidate_visibility_cache()
    _delayed_invalidate_cache()
    # 团队增删改会影响"授权部门下的活跃团队"集合（L3 来源之一），全量失效 L2/L3/L4
    _invalidate_all_scope()


@receiver(post_delete, sender=Team)
def team_post_delete(sender, instance, **kwargs):
    """团队删除后清除可见性缓存（延迟双删）"""
    _invalidate_visibility_cache()
    _delayed_invalidate_cache()
    _invalidate_all_scope()


# ── 节点树同步 ──────────────────────────────────────────────
# 部门/团队变动时同步更新 KnowledgeNode 树

@receiver(post_save, sender=Department)
def on_department_node_sync(sender, instance, **kwargs):
    """部门保存 → 同步知识节点树（带递归保护）

    sync_dept_node 内部可能保存 Department 实例（如同步 code/name），
    触发 post_save 再次进入本信号。使用 thread-local 标记防递归。
    """
    if kwargs.get('raw', False):
        return
    guard_key = f'dept_sync_{instance.pk}'
    if getattr(_sync_guard, guard_key, False):
        return
    try:
        setattr(_sync_guard, guard_key, True)
        from apps.knowledge.node_sync import sync_dept_node
        sync_dept_node(instance)
    except Exception as e:
        logger.error(f'[Signal] 部门节点同步失败: {e}')
    finally:
        setattr(_sync_guard, guard_key, False)


@receiver(post_save, sender=Team)
def on_team_node_sync(sender, instance, **kwargs):
    """团队保存 → 同步知识节点树（带递归保护）

    sync_team_node 内部可能保存 Team 实例（如同步 code/name），
    触发 post_save 再次进入本信号。使用 thread-local 标记防递归。
    """
    if kwargs.get('raw', False):
        return
    guard_key = f'team_sync_{instance.pk}'
    if getattr(_sync_guard, guard_key, False):
        return
    try:
        setattr(_sync_guard, guard_key, True)
        from apps.knowledge.node_sync import sync_team_node
        sync_team_node(instance)
    except Exception as e:
        logger.error(f'[Signal] 团队节点同步失败: {e}')
    finally:
        setattr(_sync_guard, guard_key, False)


@receiver(post_save, sender=User)
def on_user_create_init_memory(sender, instance, created, **kwargs):
    """用户创建时初始化 UserMemory，从 User 表读取基础信息"""
    if kwargs.get('raw', False):
        return
    if not created:
        return
    try:
        from apps.memory.models import UserMemory
        um, um_created = UserMemory.objects.get_or_create(user=instance)
        if um_created:
            parts = []
            if instance.real_name:
                parts.append(f"姓名：{instance.real_name}")
            if instance.department:
                parts.append(f"部门：{instance.department.name}")
            if instance.team:
                parts.append(f"团队：{instance.team.name}")
            if parts:
                um.profile_text = "；".join(parts)
                um.save()
            logger.info(f'[Signal] 初始化 UserMemory: user_id={instance.id}')
    except Exception as e:
        logger.error(f'[Signal] UserMemory 初始化失败: {e}')


@receiver(post_delete, sender=User)
def on_user_delete_clean_memory(sender, instance, **kwargs):
    """用户删除时清理相关记忆数据

    使用 bulk delete 替代逐条删除，减少 DB 查询次数。
    ShortTermMemory 无法批量清理，仍逐个会话处理。
    """
    try:
        from apps.memory.models import UserMemory, SessionMemory, Session
        from apps.memory.short_term import ShortTermMemory

        UserMemory.objects.filter(user=instance).delete()

        sessions = list(Session.objects.filter(user=instance).values_list('id', flat=True))
        if sessions:
            # 批量删除 SessionMemory（一条 SQL 替代 N 条）
            SessionMemory.objects.filter(session_id__in=sessions).delete()
            # ShortTermMemory 存在 Redis，无法批量，逐个清理
            stm = ShortTermMemory()
            for sid in sessions:
                try:
                    stm.clear(sid)
                except Exception as e:
                    logger.error(f'[Signal] 清理短期记忆失败: session_id={sid}, error={e}')

        logger.info(f'[Signal] 清理用户记忆: user_id={instance.id}')
    except Exception as e:
        logger.error(f'[Signal] 用户记忆清理失败: {e}')


# ── 权限缓存失效（L1~L4 延迟双删）─────────────────────────────
# 授权表 / 角色权限绑定变更时，精准失效对应用户的权限缓存，
# 避免鉴权层读到过期权限集合（对齐 RAG_RBAC_权限架构设计.md 第十章失效映射表）。

def _safe_invalidate_user(user_id):
    """安全失效某用户的 L1~L4 缓存 —— 失败仅记日志不阻断主业务

    审计与权限写入是主流程，缓存失效为旁路增强，任何异常都不应回滚事务。
    """
    try:
        from apps.users.perm_cache import invalidate_user_perms
        invalidate_user_perms(user_id)
    except Exception as e:
        logger.error(f'[Signal] 用户权限缓存失效失败: user_id={user_id}, error={e}')


def _safe_invalidate_role(role_id):
    """安全失效某角色绑定所有用户的 L1 缓存 —— 角色权限点变更影响面大"""
    try:
        from apps.users.perm_cache import invalidate_role_perms
        invalidate_role_perms(role_id)
    except Exception as e:
        logger.error(f'[Signal] 角色权限缓存失效失败: role_id={role_id}, error={e}')


@receiver(post_save, sender=UserRoleRel)
def on_user_role_rel_changed(sender, instance, **kwargs):
    """全局角色授权变更 → 失效该用户 L1, L4（功能权限并集 + 数据范围等级）"""
    if kwargs.get('raw', False):
        return
    _safe_invalidate_user(instance.user_id)


@receiver(post_delete, sender=UserRoleRel)
def on_user_role_rel_deleted(sender, instance, **kwargs):
    """全局角色授权删除 → 失效该用户 L1, L4"""
    _safe_invalidate_user(instance.user_id)


@receiver(post_save, sender=UserDeptScopeRel)
def on_user_dept_scope_rel_changed(sender, instance, **kwargs):
    """部门属地授权变更 → 失效该用户 L1, L2, L4（含可见部门集合）"""
    if kwargs.get('raw', False):
        return
    _safe_invalidate_user(instance.user_id)


@receiver(post_delete, sender=UserDeptScopeRel)
def on_user_dept_scope_rel_deleted(sender, instance, **kwargs):
    """部门属地授权删除 → 失效该用户 L1, L2, L4"""
    _safe_invalidate_user(instance.user_id)


@receiver(post_save, sender=UserTeamScopeRel)
def on_user_team_scope_rel_changed(sender, instance, **kwargs):
    """团队属地授权变更 → 失效该用户 L1, L3, L4（含可见团队集合）"""
    if kwargs.get('raw', False):
        return
    _safe_invalidate_user(instance.user_id)


@receiver(post_delete, sender=UserTeamScopeRel)
def on_user_team_scope_rel_deleted(sender, instance, **kwargs):
    """团队属地授权删除 → 失效该用户 L1, L3, L4"""
    _safe_invalidate_user(instance.user_id)


@receiver(post_save, sender=RolePermissionRel)
def on_role_permission_rel_changed(sender, instance, **kwargs):
    """角色-权限点绑定变更 → 失效所有持有该 role 用户的 L1（批量反查）

    角色权限点改动影响所有拥有该角色的用户，必须批量失效其功能权限并集缓存。
    """
    if kwargs.get('raw', False):
        return
    _safe_invalidate_role(instance.role_id)


@receiver(post_delete, sender=RolePermissionRel)
def on_role_permission_rel_deleted(sender, instance, **kwargs):
    """角色-权限点绑定删除 → 失效所有持有该 role 用户的 L1"""
    _safe_invalidate_role(instance.role_id)


@receiver(post_save, sender=User)
def on_user_org_changed(sender, instance, **kwargs):
    """用户调岗（department_id / team_id 变化）→ 失效 L1, L2, L3, L4 全部

    人事归属变化会影响自然可见范围与数据范围等级，需全量失效该用户权限缓存。
    仅在更新时触发（created 时无旧值可比，且新用户无缓存）。
    优化：当 update_fields 存在且不包含组织相关字段时跳过，避免更新 last_login_at
    / password / real_name 等无关字段时触发无意义的缓存失效。
    """
    if kwargs.get('raw', False) or kwargs.get('created', False):
        return
    # update_fields 存在时仅在包含组织/角色相关字段时触发缓存失效
    update_fields = kwargs.get('update_fields')
    if update_fields is not None:
        # 组织归属 + 角色相关字段集合（变化会影响权限缓存）
        perm_related = {'department_id', 'team', 'status', 'is_deleted'}
        if not (perm_related & set(update_fields)):
            return
    _safe_invalidate_user(instance.id)


@receiver(post_save, sender=Role)
def on_role_data_scope_changed(sender, instance, **kwargs):
    """角色 data_scope 变化 → 全量失效 L2/L3/L4

    data_scope 决定持有该角色用户的最高数据范围等级（L4），角色变更无法
    精确定位受影响用户，全量保守过失效。角色保存低频（工单/超管操作），
    成本可接受；update_fields 不含 data_scope 或全量保存时也保守失效，
    避免漏判（角色字段仅 name/description 变化时多余失效无害）。
    """
    if kwargs.get('raw', False) or kwargs.get('created', False):
        return
    _invalidate_all_scope()
