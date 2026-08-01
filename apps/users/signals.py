"""
部门/团队缓存失效 + 节点树同步
使用延迟双删策略：
1. 修改后立即删除缓存
2. 延迟5秒再次删除缓存（防止并发问题）
"""
from loguru import logger
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.core.cache import cache

from .models import Department, Team


def _invalidate_visibility_cache():
    """删除所有allowed_visibility相关的缓存和部门列表缓存"""
    try:
        # 使用Redis的scan命令查找所有匹配的key
        from django_redis import get_redis_connection
        conn = get_redis_connection('default')
        deleted_count = 0
        for key in conn.scan_iter('allowed_visibility_*'):
            conn.delete(key)
            deleted_count += 1
        conn.delete('available_depts_list')
        logger.debug(f"Invalidated {deleted_count} visibility cache keys and available_depts_list")
    except ImportError:
        # django_redis 不可用（非Redis缓存）
        logger.debug("django_redis not available, skipping cache invalidation")
    except Exception as e:
        # 其他错误，尝试使用delete_pattern（仅Redis支持）
        try:
            cache.delete_pattern('allowed_visibility_*')
            cache.delete('available_depts_list')
            logger.debug("Invalidated visibility cache via delete_pattern")
        except AttributeError:
            # LocMemCache不支持delete_pattern
            logger.debug("Cache backend does not support delete_pattern, skipping cache invalidation")
        except Exception as e2:
            logger.error(f"Failed to invalidate visibility cache: {e2}")


def _delayed_invalidate_cache(delay=5):
    """延迟删除缓存（延迟双删）"""
    try:
        # 尝试使用Celery
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


@receiver(post_save, sender=Department)
def department_post_save(sender, instance, created, **kwargs):
    """部门保存后清除缓存"""
    _invalidate_visibility_cache()
    _delayed_invalidate_cache()


@receiver(post_delete, sender=Department)
def department_post_delete(sender, instance, **kwargs):
    """部门删除后清除缓存"""
    _invalidate_visibility_cache()
    _delayed_invalidate_cache()


@receiver(post_save, sender=Team)
def team_post_save(sender, instance, created, **kwargs):
    """团队保存后清除缓存"""
    _invalidate_visibility_cache()
    _delayed_invalidate_cache()


@receiver(post_delete, sender=Team)
def team_post_delete(sender, instance, **kwargs):
    """团队删除后清除缓存"""
    _invalidate_visibility_cache()
    _delayed_invalidate_cache()


# ── 节点树同步 ──────────────────────────────────────────────
# 部门/团队变动时同步更新 KnowledgeNode 树

@receiver(post_save, sender=Department)
def on_department_node_sync(sender, instance, **kwargs):
    """部门保存 → 同步知识节点树"""
    if kwargs.get('raw', False):
        return
    try:
        from apps.knowledge.node_sync import sync_dept_node
        sync_dept_node(instance)
    except Exception as e:
        logger.error(f'[Signal] 部门节点同步失败: {e}')


@receiver(post_save, sender=Team)
def on_team_node_sync(sender, instance, **kwargs):
    """团队保存 → 同步知识节点树"""
    if kwargs.get('raw', False):
        return
    try:
        from apps.knowledge.node_sync import sync_team_node
        sync_team_node(instance)
    except Exception as e:
        logger.error(f'[Signal] 团队节点同步失败: {e}')


@receiver(post_save, sender=__import__('apps.users.models').users.models.User)
def on_user_create_init_memory(sender, instance, created, **kwargs):
    """用户创建时初始化 UserMemory，从 User 表读取基础信息"""
    if kwargs.get('raw', False):
        return
    if not created:
        return
    try:
        from apps.memory.models import UserMemory
        um, created = UserMemory.objects.get_or_create(user=instance)
        if created:
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


@receiver(post_delete, sender=__import__('apps.users.models').users.models.User)
def on_user_delete_clean_memory(sender, instance, **kwargs):
    """用户删除时清理相关记忆数据"""
    try:
        from apps.memory.models import UserMemory, SessionMemory, Session
        from apps.memory.short_term import ShortTermMemory

        UserMemory.objects.filter(user=instance).delete()

        sessions = Session.objects.filter(user=instance)
        for sess in sessions:
            try:
                SessionMemory.objects.filter(session=sess).delete()
                ShortTermMemory().clear(sess.id)
            except Exception as e:
                logger.error(f'[Signal] 清理会话记忆失败: session_id={sess.id}, error={e}')

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


@receiver(post_save, sender=__import__('apps.users.models').users.models.UserRoleRel)
def on_user_role_rel_changed(sender, instance, **kwargs):
    """全局角色授权变更 → 失效该用户 L1, L4（功能权限并集 + 数据范围等级）"""
    if kwargs.get('raw', False):
        return
    _safe_invalidate_user(instance.user_id)


@receiver(post_delete, sender=__import__('apps.users.models').users.models.UserRoleRel)
def on_user_role_rel_deleted(sender, instance, **kwargs):
    """全局角色授权删除 → 失效该用户 L1, L4"""
    _safe_invalidate_user(instance.user_id)


@receiver(post_save, sender=__import__('apps.users.models').users.models.UserDeptScopeRel)
def on_user_dept_scope_rel_changed(sender, instance, **kwargs):
    """部门属地授权变更 → 失效该用户 L1, L2, L4（含可见部门集合）"""
    if kwargs.get('raw', False):
        return
    _safe_invalidate_user(instance.user_id)


@receiver(post_delete, sender=__import__('apps.users.models').users.models.UserDeptScopeRel)
def on_user_dept_scope_rel_deleted(sender, instance, **kwargs):
    """部门属地授权删除 → 失效该用户 L1, L2, L4"""
    _safe_invalidate_user(instance.user_id)


@receiver(post_save, sender=__import__('apps.users.models').users.models.UserTeamScopeRel)
def on_user_team_scope_rel_changed(sender, instance, **kwargs):
    """团队属地授权变更 → 失效该用户 L1, L3, L4（含可见团队集合）"""
    if kwargs.get('raw', False):
        return
    _safe_invalidate_user(instance.user_id)


@receiver(post_delete, sender=__import__('apps.users.models').users.models.UserTeamScopeRel)
def on_user_team_scope_rel_deleted(sender, instance, **kwargs):
    """团队属地授权删除 → 失效该用户 L1, L3, L4"""
    _safe_invalidate_user(instance.user_id)


@receiver(post_save, sender=__import__('apps.users.models').users.models.RolePermissionRel)
def on_role_permission_rel_changed(sender, instance, **kwargs):
    """角色-权限点绑定变更 → 失效所有持有该 role 用户的 L1（批量反查）

    角色权限点改动影响所有拥有该角色的用户，必须批量失效其功能权限并集缓存。
    """
    if kwargs.get('raw', False):
        return
    _safe_invalidate_role(instance.role_id)


@receiver(post_delete, sender=__import__('apps.users.models').users.models.RolePermissionRel)
def on_role_permission_rel_deleted(sender, instance, **kwargs):
    """角色-权限点绑定删除 → 失效所有持有该 role 用户的 L1"""
    _safe_invalidate_role(instance.role_id)


@receiver(post_save, sender=__import__('apps.users.models').users.models.User)
def on_user_org_changed(sender, instance, **kwargs):
    """用户调岗（department_id / team_id 变化）→ 失效 L1, L2, L3, L4 全部

    人事归属变化会影响自然可见范围与数据范围等级，需全量失效该用户权限缓存。
    仅在更新时触发（created 时无旧值可比，且新用户无缓存）。
    """
    if kwargs.get('raw', False) or kwargs.get('created', False):
        return
    _safe_invalidate_user(instance.id)
