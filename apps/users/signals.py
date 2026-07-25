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
    """删除所有allowed_visibility相关的缓存"""
    try:
        # 使用Redis的scan命令查找所有匹配的key
        from django_redis import get_redis_connection
        conn = get_redis_connection('default')
        deleted_count = 0
        for key in conn.scan_iter('allowed_visibility_*'):
            conn.delete(key)
            deleted_count += 1
        logger.debug(f"Invalidated {deleted_count} visibility cache keys")
    except ImportError:
        # django_redis 不可用（非Redis缓存）
        logger.debug("django_redis not available, skipping cache invalidation")
    except Exception as e:
        # 其他错误，尝试使用delete_pattern（仅Redis支持）
        try:
            cache.delete_pattern('allowed_visibility_*')
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
