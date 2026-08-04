# Generated for: 配置工单增加变更摘要字段，用于多值配置（如 BUSINESS_DB_TABLES）的差异展示

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('system', '0008_llm_model_timeout'),
    ]

    operations = [
        migrations.AddField(
            model_name='configchangeticket',
            name='change_summary',
            field=models.TextField(blank=True, default='',
                                    help_text='多值配置的差异信息 JSON：{added:[], removed:[]}',
                                    verbose_name='变更摘要'),
        ),
    ]
