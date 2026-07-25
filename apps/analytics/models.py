"""
analytics app - 关键词权重 & 准确率日报 Model
对齐数据库设计 E3/E4
KeywordWeight 支持基于历史准确率的动态关键词加权
"""
from django.db import models
from django.contrib.postgres.fields import ArrayField


class KeywordWeight(models.Model):
    """E3 keyword_weight - 关键词权重表
    BM25 分数按 weight_score 加权，使高价值关键词权重提升"""

    id = models.BigAutoField(primary_key=True)
    keyword = models.CharField(max_length=64)
    root_type = models.CharField(max_length=32, default='all')
    hit_count = models.IntegerField(default=0)
    good_feedback = models.IntegerField(default=0)
    bad_feedback = models.IntegerField(default=0)
    weight_score = models.FloatField(default=1.0, help_text='初始 1.0，好评 +0.1，差评 -0.1')
    last_hit_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'analytics_keyword_weight'
        unique_together = [('keyword', 'root_type')]
        indexes = [
            models.Index(fields=['keyword'], name='idx_kw_kw'),
            models.Index(fields=['-weight_score'], name='idx_kw_score'),
        ]


class AccuracyReport(models.Model):
    """E4 accuracy_report - 准确率日报
    每天 02:00 Celery Beat 统计前一天数据"""

    id = models.BigAutoField(primary_key=True)
    report_date = models.DateField(unique=True)
    total_qa = models.IntegerField(default=0)
    good_count = models.IntegerField(default=0)
    bad_count = models.IntegerField(default=0)
    no_feedback_count = models.IntegerField(default=0)
    accuracy_rate = models.FloatField(default=0.0, help_text='good / (good+bad)')
    avg_latency_ms = models.IntegerField(default=0)
    total_tokens = models.BigIntegerField(default=0)
    total_cost = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    top_bad_tags = models.JSONField(default=list, blank=True,
                                     help_text='[{tag, count}] 差评标签 Top 5')
    top_root_types = models.JSONField(default=list, blank=True,
                                       help_text='[{root_type, qa_count}]')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'analytics_accuracy_report'
        indexes = [
            models.Index(fields=['-report_date'], name='idx_ar_date'),
        ]
