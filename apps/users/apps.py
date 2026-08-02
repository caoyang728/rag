from django.apps import AppConfig


class UsersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.users'
    label = 'users'

    def ready(self):
        # 注册信号处理器
        import apps.users.signals
        # 注册 Django 系统检查(部署期硬约束:超管数量等)
        from apps.users import checks  # noqa: F401
