# GoldenQuestion.source_qa_record_id 加唯一约束:
# 低分回归沉淀在并发执行(批量任务重复调度)时存在竞态窗口,
# 应用层 existing_qa_ids 快照不能兜底,依赖 DB 唯一约束保证同一低分对话只沉淀一次。

from django.db import migrations, models


def dedupe_regression_questions(apps, schema_editor):
    """历史数据去重:同一 source_qa_record_id 只保留 id 最小的一行

    约束建立在已有数据之上,若历史存在重复沉淀需先清理,否则 AddConstraint 会失败。
    """
    GoldenQuestion = apps.get_model('analytics', 'GoldenQuestion')
    seen = set()
    dup_ids = []
    for gq in (
        GoldenQuestion.objects
        .filter(source_qa_record_id__isnull=False)
        .order_by('id')
    ):
        if gq.source_qa_record_id in seen:
            dup_ids.append(gq.id)
        else:
            seen.add(gq.source_qa_record_id)
    if dup_ids:
        GoldenQuestion.objects.filter(id__in=dup_ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('analytics', '0011_systemmetricsreport_cache_hit_p99_latency_and_more'),
    ]

    operations = [
        migrations.RunPython(dedupe_regression_questions, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='goldenquestion',
            constraint=models.UniqueConstraint(
                condition=~models.Q(('source_qa_record_id', None)),
                fields=('source_qa_record_id',),
                name='uniq_gq_source_qa_record',
            ),
        ),
    ]
