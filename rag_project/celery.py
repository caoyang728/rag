"""
Celery 应用配置。六个队列：default / parse / memory / email / analytics
用法：celery -A rag_project worker -l info -Q default,parse,memory,email,analytics

- analytics 队列独立出来，避免后台监控任务与业务问答任务争抢 Worker
- 若部署单 Worker，可合并：celery -A rag_project worker -l info
- 若部署独立监控 Worker：celery -A rag_project worker -l info -Q analytics
"""
import os

from celery import Celery

from apps.system.scheduler_registry import default_schedule_dict

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'rag_project.settings')

app = Celery('rag_agent')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

# ---- 定时任务（Beat）----
# 调度时间不再写死于此：任务清单与默认 cron 收敛在 apps/system/scheduler_registry.py，
# 管理端"定时任务"页面可修改各任务的调度时间与启停状态（写入 SystemConfig 并走工单审批）。
# 运行期热更新由 SystemConfigScheduler 完成（见 settings.CELERY_BEAT_SCHEDULER），
# 工单审批通过后无需重启 beat；此处仅提供 DB 不可用/未初始化时的默认兜底调度。
app.conf.beat_schedule = default_schedule_dict()


@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
