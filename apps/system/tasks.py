"""apps.system 的 Celery 任务

当前仅一个：cleanup_task_logs —— 每日清理超过保留期的任务日志，
与任务看板的"日志量受控"约束配套（写入点见 task_signals.py）。

注意：本任务不使用 @shared_task(name=...) 显式命名，让 Celery 按
模块路径（apps.system.tasks.cleanup_task_logs）注册，与
scheduler_registry.SCHEDULED_TASKS 中的 task 路径保持一致，
保证 beat 按路径名派发后 Worker 能正确解析执行。
"""
from datetime import timedelta

from celery import shared_task
from django.utils import timezone
from loguru import logger

from apps.system.models import CeleryTaskLog, SystemConfig

# 任务日志默认保留天数：超期日志每日清理，控制看板数据量
_DEFAULT_RETENTION_DAYS = 30

# 单批删除上限：分批删除避免一次性删除大量行产生长事务阻塞 Worker
_BATCH_SIZE = 2000


def _retention_days():
    """从 SystemConfig 读取日志保留天数（缺失/非法时回退默认 30 天）"""
    try:
        row = SystemConfig.objects.filter(key='TASK_LOG_RETENTION_DAYS').only('value').first()
        if row:
            days = int(str(row.value).strip())
            if 1 <= days <= 365:
                return days
    except Exception as e:
        logger.warning(f'[TaskLog] 读取保留天数失败，使用默认 {_DEFAULT_RETENTION_DAYS}: {e}')
    return _DEFAULT_RETENTION_DAYS


@shared_task(queue='default')
def cleanup_task_logs():
    """清理超过保留期的任务日志（每日定时执行，数据量控制）

    - 按 created_at 截止时间分批删除，每次最多 _BATCH_SIZE 行，
      直到删完或为空（防止长事务）
    - 保留期可通过 SystemConfig.TASK_LOG_RETENTION_DAYS 调整（默认 30 天）
    """
    retention = _retention_days()
    deadline = timezone.now() - timedelta(days=retention)
    deleted_total = 0
    while True:
        ids = list(CeleryTaskLog.objects.filter(created_at__lt=deadline)
                   .values_list('id', flat=True)[:_BATCH_SIZE])
        if not ids:
            break
        deleted_total += CeleryTaskLog.objects.filter(id__in=ids).delete()[0]
    logger.info(f'[TaskLog] 清理完成: 删除 {deleted_total} 条超过 {retention} 天的任务日志')
    return {'deleted': deleted_total}
