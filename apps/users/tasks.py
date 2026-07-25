"""
用户模块的 Celery 任务
"""
from celery import shared_task

from .signals import _invalidate_visibility_cache


@shared_task
def delayed_invalidate_visibility_cache():
    """延迟删除部门/团队可见性缓存（延迟双删）"""
    _invalidate_visibility_cache()
