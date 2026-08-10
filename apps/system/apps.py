from django.apps import AppConfig


class SystemConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.system'
    label = 'system'

    def ready(self):
        # 注册 Celery 任务信号：统一写入 CeleryTaskLog（后台任务看板数据源）
        import apps.system.task_signals  # noqa: F401
