"""Celery 任务信号 —— 统一写入 CeleryTaskLog（后台任务看板数据源）

背景：CeleryTaskLog 模型已存在但此前无写入点。本模块通过 Celery 的
task_prerun / task_postrun / task_failure / task_revoked 四个信号，
把每次任务执行的生命周期状态落到该表，供管理端"任务看板"页面实时展示。

设计约束（对应 plan【14】验收标准）：
- 幂等：task_id 唯一索引，全部用 update_or_create / 按 task_id 更新，
  同一任务重试或信号重入不会产生重复行。
- 不阻塞：信号内所有 ORM 写操作包在 try/except 中，失败仅记日志，
  绝不影响任务主流程执行。
- 防暴涨：高频维护类任务（每 5 分钟的队列深度快照/实时指标刷新/
  IP 封禁过期清理）不进日志；另有每日 cleanup_task_logs 定时任务
  清理超过保留期的历史日志（默认 30 天），保证日志量受控。
- 字段截断：result / error_message 超长截断，避免大对象拖库。
"""
import json

from celery.signals import task_failure, task_postrun, task_prerun, task_revoked
from django.utils import timezone
from loguru import logger

from apps.system.models import CeleryTaskLog

# 高频维护类任务不记录：每 5 分钟执行一次，对看板无运维价值且会淹没列表。
# 同时列出模块路径与注册短名两种形式，兼容 beat 派发（模块路径）与直接调用（短名）。
_SKIP_TASKS = frozenset({
    'rag_project.celery.debug_task',                        # Celery 自带调试任务
    'apps.analytics.tasks.update_queue_depth_snapshot',     # 队列深度快照（5min）
    'analytics.update_queue_depth',
    'apps.analytics.tasks.flush_realtime_metrics_task',     # 实时指标刷新（5min）
    'analytics.flush_realtime',
    'apps.security.tasks.expire_ip_blacklist',              # IP 封禁过期清理（5min）
    'security.expire_ip_blacklist',
    'apps.users.perm_cache.delayed_delete_keys',           # 权限缓存延迟删除（权限变更即触发）
})

# result / error_message 存储上限：超出截断，控制单行体积
_MAX_TEXT_LEN = 2000


def _is_recordable(task_name):
    """是否记录该任务：跳过高频维护类任务与调试任务，其余任务均记录"""
    return bool(task_name) and task_name not in _SKIP_TASKS


def _queue_of(sender):
    """从任务请求的 delivery_info 提取队列名（缺失时回退 default）"""
    try:
        info = sender.request.delivery_info or {}
        return info.get('routing_key') or 'default'
    except Exception:
        return 'default'


def _safe_args(value):
    """把任务 args 转为可 JSON 序列化列表（非法值回退空列表，不阻断信号）"""
    try:
        return json.loads(json.dumps(value or [], ensure_ascii=False, default=str))
    except Exception:
        return []


def _safe_kwargs(value):
    """把任务 kwargs 转为可 JSON 序列化 dict（非法值回退空 dict，不阻断信号）"""
    try:
        return json.loads(json.dumps(value or {}, ensure_ascii=False, default=str))
    except Exception:
        return {}


def _safe_text(value, limit=_MAX_TEXT_LEN):
    """文本截断存储，避免 result / error_message 超长拖库"""
    return str(value or '')[:limit]


def _record_started(task_id, task_name, queue, args, kwargs, retries):
    """prerun：写入/更新为 started 状态（幂等，按 task_id 唯一）"""
    CeleryTaskLog.objects.update_or_create(
        task_id=task_id,
        defaults={
            'task_name': task_name,
            'queue': queue,
            'args': args,
            'kwargs': kwargs,
            'status': 'started',
            'started_at': timezone.now(),
            'retry_count': retries,
            # 重试时清空上一轮的结果/错误，避免看板残留旧信息
            'result': '',
            'error_message': '',
            'finished_at': None,
            'duration_ms': 0,
        },
    )


def _record_finished(task_id, status, result='', error_message=''):
    """postrun/failure/revoked 公共落库：按 task_id 更新终态与耗时

    若 prerun 未写入（如被跳过的任务或临时写库失败），此处不再新造行，
    避免产生缺 args/kwargs 的半截记录。
    """
    row = CeleryTaskLog.objects.filter(task_id=task_id).first()
    if row is None:
        return
    now = timezone.now()
    started = row.started_at or row.created_at
    duration_ms = int((now - started).total_seconds() * 1000) if started else 0
    row.status = status
    row.finished_at = now
    row.duration_ms = duration_ms
    if result:
        row.result = _safe_text(result)
    if error_message:
        row.error_message = _safe_text(error_message)
    row.save(update_fields=['status', 'finished_at', 'duration_ms', 'result', 'error_message'])


def on_task_prerun(sender, task_id, task, args, kwargs, **extra):
    """task_prerun：任务开始执行时记录 started 状态"""
    try:
        if not _is_recordable(task):
            return
        retries = 0
        try:
            retries = sender.request.retries or 0
        except Exception:
            retries = 0
        _record_started(task_id, task, _queue_of(sender),
                        _safe_args(args), _safe_kwargs(kwargs), retries)
    except Exception as e:
        logger.warning(f'[TaskLog] prerun 写入失败 task={task} id={task_id}: {e}')


def on_task_postrun(sender, task_id, task, retval, **extra):
    """task_postrun：任务成功返回后记录 success 状态与耗时"""
    try:
        if not _is_recordable(task):
            return
        try:
            result = json.dumps(retval, ensure_ascii=False, default=str)
        except Exception:
            result = ''
        _record_finished(task_id, 'success', result=result)
    except Exception as e:
        logger.warning(f'[TaskLog] postrun 写入失败 task={task} id={task_id}: {e}')


def on_task_failure(sender, task_id, task, exception, traceback, **extra):
    """task_failure：任务抛异常后记录 failure 状态与错误信息"""
    try:
        if not _is_recordable(task):
            return
        message = str(exception or '')
        if traceback:
            message = f'{message}\n{traceback}'
        _record_finished(task_id, 'failure', error_message=message)
    except Exception as e:
        logger.warning(f'[TaskLog] failure 写入失败 task={task} id={task_id}: {e}')


def on_task_revoked(sender, request, terminated, signum, expired, **extra):
    """task_revoked：任务被撤销后记录 revoked 状态"""
    try:
        task_id = getattr(request, 'id', None)
        task = getattr(request, 'task', '')
        if not task_id or not _is_recordable(task):
            return
        _record_finished(task_id, 'revoked')
    except Exception as e:
        logger.warning(f'[TaskLog] revoked 写入失败 task={task} id={task_id}: {e}')


# 模块导入时注册信号（apps.system.apps.SystemConfig.ready 中 import 本模块）
task_prerun.connect(on_task_prerun)
task_postrun.connect(on_task_postrun)
task_failure.connect(on_task_failure)
task_revoked.connect(on_task_revoked)
