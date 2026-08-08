"""添加 UnifiedTicket.updated_at —— 工单更新时间的展示与排序

存量迁移（0009）回填 created_at 时使用 update() 会跳过 auto_now_add，
updated_at 由 auto_now 在后续每次 save 时自动维护，无需数据回填。
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0010_alter_auth_ticket_fk_and_backfill'),
    ]

    operations = [
        migrations.AddField(
            model_name='unifiedticket',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, verbose_name='更新时间'),
        ),
    ]
