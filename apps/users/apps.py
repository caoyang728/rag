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

        # Monkey-patch GenericIPAddressField.get_prep_value
        # Django 5.2 从 DB 加载 GenericIPAddressField 时返回 IPv4Address/IPv6Address 对象，
        # 但 get_prep_value 中 ":" in value 对 IPv4Address 会触发 TypeError（不支持 __contains__）。
        # 在此统一将 value 转为 str 后再做 IPv6 检测，兼容 IPv4Address/IPv6Address 对象。
        from django.db.models import GenericIPAddressField
        from django.utils.ipv6 import clean_ipv6_address

        _orig_get_prep_value = GenericIPAddressField.get_prep_value

        def _patched_get_prep_value(self, value):
            if value is None:
                return None
            # 先转为字符串，兼容 IPv4Address/IPv6Address 对象
            value = str(value)
            if ":" in value:
                try:
                    return clean_ipv6_address(value, self.unpack_ipv4)
                except Exception:
                    pass
            return value

        GenericIPAddressField.get_prep_value = _patched_get_prep_value
