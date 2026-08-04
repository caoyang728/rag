# 移除已废弃的 PRODUCTION_EVAL_METRIC_GROUPS 配置项
# 该配置原用于按指标组（core/retrieval/safety/quality/business）控制评估范围，
# 现已统一收敛到 EVAL_DISPLAY_DIMENSIONS（按维度独立控制，评估=展示强绑定）。
# 老部署升级后需清理 SystemConfig 中的残留记录，避免管理员在前端看到无效配置项。

from django.db import migrations


def remove_deprecated_config(apps, schema_editor):
    """删除 SystemConfig 表中 key=PRODUCTION_EVAL_METRIC_GROUPS 的记录"""
    SystemConfig = apps.get_model('system', 'SystemConfig')
    SystemConfig.objects.filter(key='PRODUCTION_EVAL_METRIC_GROUPS').delete()


def noop_reverse(apps, schema_editor):
    """反向操作无意义：配置项已废弃，不恢复"""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('system', '0010_modelchangeticket'),
    ]

    operations = [
        migrations.RunPython(remove_deprecated_config, noop_reverse),
    ]
