# 删除死代码 AccuracyReport 模型:
# 该表仅存在于旧版本统计链路,生产代码(views/services/tasks/serializers)
# 已无任何引用,仅剩模型定义与旧迁移,保留无意义且误导后续开发。

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('analytics', '0012_goldenquestion_unique_source_qa'),
    ]

    operations = [
        migrations.DeleteModel(
            name='AccuracyReport',
        ),
    ]
