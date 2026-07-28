"""
记忆清理信号
- 删除会话时清理短时记忆
- 更新 GlobalMemory 时清除全局缓存
"""
from loguru import logger
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Session, GlobalMemory
from .short_term import ShortTermMemory


@receiver(post_delete, sender=Session)
def on_session_delete_clean_short_term(sender, instance, **kwargs):
    """会话删除时清理短时记忆"""
    try:
        ShortTermMemory().clear(instance.id)
        logger.info(f'[Signal] 清理会话短时记忆: session_id={instance.id}')
    except Exception as e:
        logger.error(f'[Signal] 会话短时记忆清理失败: {e}')


@receiver(post_save, sender=GlobalMemory)
def on_global_memory_change_clear_cache(sender, instance, **kwargs):
    """GlobalMemory 更新时清除全局缓存"""
    try:
        from apps.memory.manager import MemoryManager
        MemoryManager._global_cache = {}
        MemoryManager._global_cache_time = 0
        logger.info(f'[Signal] 清除全局记忆缓存: key={instance.key}')
    except Exception as e:
        logger.error(f'[Signal] 清除全局缓存失败: {e}')