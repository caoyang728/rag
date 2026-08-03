"""
Celery 应用配置。六个队列：default / parse / memory / email / analytics
用法：celery -A rag_project worker -l info -Q default,parse,memory,email,analytics

- analytics 队列独立出来，避免后台监控任务与业务问答任务争抢 Worker
- 若部署单 Worker，可合并：celery -A rag_project worker -l info
- 若部署独立监控 Worker：celery -A rag_project worker -l info -Q analytics
"""
import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'rag_project.settings')

app = Celery('rag_agent')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

# ---- 定时任务（Beat）----
app.conf.beat_schedule = {
    # 每日 02:00 聚合前一天系统指标（P50/P95/P99、缓存命中率、错误率等）
    'system-metrics-daily': {
        'task': 'apps.analytics.tasks.compute_system_metrics_daily',
        'schedule': crontab(hour=2, minute=0),
    },
    # 每日 02:10 聚合前一天组织使用数据（部门/团队对话、Token、费用）
    'org-usage-daily': {
        'task': 'apps.analytics.tasks.compute_org_usage_daily',
        'schedule': crontab(hour=2, minute=10),
    },
    # 每 5 分钟更新队列深度快照（PG 历史 + Redis 实时）
    'queue-depth-snapshot': {
        'task': 'apps.analytics.tasks.update_queue_depth_snapshot',
        'schedule': crontab(minute='*/5'),
    },
    # 每 5 分钟刷新实时指标时间戳
    'realtime-metrics-flush': {
        'task': 'apps.analytics.tasks.flush_realtime_metrics_task',
        'schedule': crontab(minute='*/5'),
    },
    # 每 5 分钟清理过期临时 IP 封禁
    'expire-ip-blacklist': {
        'task': 'apps.security.tasks.expire_ip_blacklist',
        'schedule': crontab(minute='*/5'),
    },
    # 每晚提炼稳定的用户偏好
    'refine-user-memory': {
        'task': 'apps.memory.tasks.refine_user_memory',
        'schedule': crontab(hour=2, minute=30),
    },
    # 每小时处理未处理的差评反馈
    'handle-feedback': {
        'task': 'apps.chat.tasks.handle_feedback',
        'schedule': crontab(minute=15),
    },
    # 每日 03:30 清理过期监控数据（低峰期）
    'cleanup-old-analytics-data': {
        'task': 'apps.analytics.tasks.cleanup_old_data',
        'schedule': crontab(hour=3, minute=30),
    },
    # 每日 04:00 批量评估文档质量
    'doc-quality-daily': {
        'task': 'apps.analytics.tasks.batch_evaluate_document_quality',
        'schedule': crontab(hour=4, minute=0),
    },
    # 每日 04:30 生成知识库覆盖率报告
    'coverage-report-daily': {
        'task': 'apps.analytics.tasks.generate_coverage_report_daily',
        'schedule': crontab(hour=4, minute=30),
    },
    # 每 2 小时执行多维度回答质量评估（与忠实度错开）
    'multi-dim-evaluation': {
        'task': 'apps.analytics.tasks.run_multi_dimension_evaluation',
        'schedule': crontab(hour='*/2', minute=30),
    },
    # 每周一 05:00 执行离线检索评估（黄金测试集回归测试）
    'periodic-retrieval-eval': {
        'task': 'apps.analytics.tasks.periodic_retrieval_evaluation',
        'schedule': crontab(hour=5, minute=0, day_of_week=1),
    },
}


@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
