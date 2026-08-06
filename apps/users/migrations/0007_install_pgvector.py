"""
迁移：安装 pgvector 扩展

这是必需的，因为多个 app 的模型使用了 VectorField。
使用 pgvector 官方推荐的 CreateExtension 操作。
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0006_alter_scope_rel_unique_constraints'),
    ]

    operations = [
        # 安装 pgvector 扩展，所有使用 VectorField 的 app 都依赖于此
        migrations.RunSQL(
            sql="CREATE EXTENSION IF NOT EXISTS vector;",
            reverse_sql="DROP EXTENSION IF EXISTS vector;",
        ),
    ]
