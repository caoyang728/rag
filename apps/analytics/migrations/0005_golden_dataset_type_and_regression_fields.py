# Generated for 低分回归测试集改造

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('analytics', '0004_lowscoreanalysis'),
    ]

    operations = [
        # GoldenDataset: 加 dataset_type 字段 + 索引
        migrations.AddField(
            model_name='goldendataset',
            name='dataset_type',
            field=models.CharField(
                choices=[('custom', '自定义'), ('regression_low_score', '低分回归')],
                default='custom',
                help_text='测试集类型: custom(人工维护) / regression_low_score(低分回归)',
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name='goldendataset',
            index=models.Index(
                fields=['dataset_type', '-updated_at'],
                name='idx_gds_type_time',
            ),
        ),
        # GoldenQuestion: 加低分回归专用字段 + 索引
        migrations.AddField(
            model_name='goldenquestion',
            name='source_qa_record_id',
            field=models.BigIntegerField(
                blank=True, null=True,
                help_text='低分回归专用:沉淀来源的 QaRecord.id',
            ),
        ),
        migrations.AddField(
            model_name='goldenquestion',
            name='pass_count',
            field=models.IntegerField(
                default=0,
                help_text='低分回归专用:连续通过次数,失败重置为 0',
            ),
        ),
        migrations.AddField(
            model_name='goldenquestion',
            name='last_eval_at',
            field=models.DateTimeField(
                blank=True, null=True,
                help_text='低分回归专用:最近一次回归评估时间',
            ),
        ),
        migrations.AddIndex(
            model_name='goldenquestion',
            index=models.Index(
                fields=['source_qa_record_id'],
                name='idx_gq_source_qa',
            ),
        ),
        migrations.AddIndex(
            model_name='goldenquestion',
            index=models.Index(
                fields=['pass_count'],
                name='idx_gq_pass_count',
            ),
        ),
    ]
