"""
Celery 应用配置。四个队列：default / parse / memory / email
用法：celery -A rag_project worker -l info -Q default,parse,memory,email
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
    # 每日凌晨聚合准确率日报
    'daily-accuracy-report': {
        'task': 'apps.analytics.tasks.aggregate_daily_report',
        'schedule': crontab(hour=1, minute=10),
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
}


@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
