"""
analytics app - 关键词权重 & 准确率日报 & 性能监控 Model
对齐数据库设计 E3/E4 + 系统监控域（SystemMetricsReport/OrgUsageReport/QueueDepthLog/AnswerQualityReport）
KeywordWeight 支持基于历史准确率的动态关键词加权
SystemMetricsReport: P50/P95/P99、缓存命中率、LLM 错误率等系统级日报
OrgUsageReport: 部门/团队维度的对话次数、Token 消耗、费用统计
QueueDepthLog: Celery 队列深度定时快照（每 5 分钟），用于 Dashboard 展示历史趋势
AnswerQualityReport: 回答忠实度评估报告（异步 LLM 评估）
"""
from django.db import models
from django.contrib.postgres.fields import ArrayField

from apps.chat.models import QaRecord


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


class SystemMetricsReport(models.Model):
    """系统级性能指标日报（Celery Beat 预计算，每天 02:00）

    - 每天凌晨聚合前一天数据，Dashboard 直接读取预计算结果，
      避免每次 Dashboard 加载时实时聚合数万条 QaRecord
    - 区分"缓存命中"与"正常请求"的延迟指标，防止缓存命中的亚毫秒延迟
      稀释正常请求的 P50/P95/P99，导致指标失真
    """

    id = models.BigAutoField(primary_key=True)
    report_date = models.DateField(unique=True)

    # --- 总请求数 ---
    total_qa = models.IntegerField(default=0)
    cache_hit_count = models.IntegerField(default=0, help_text='缓存命中数')
    normal_qa_count = models.IntegerField(default=0, help_text='非缓存请求数')

    # --- 正常请求延迟百分位（毫秒，不含缓存命中）---
    p50_latency_total = models.IntegerField(default=0, help_text='端到端 P50（不含缓存命中）')
    p95_latency_total = models.IntegerField(default=0, help_text='端到端 P95（不含缓存命中）')
    p99_latency_total = models.IntegerField(default=0, help_text='端到端 P99（不含缓存命中）')
    p50_latency_llm = models.IntegerField(default=0, help_text='LLM 调用 P50')
    p95_latency_llm = models.IntegerField(default=0, help_text='LLM 调用 P95')
    p50_latency_retrieval = models.IntegerField(default=0, help_text='检索 P50')
    p95_latency_retrieval = models.IntegerField(default=0, help_text='检索 P95')
    p50_ttfb = models.IntegerField(default=0, help_text='首字返回 P50')
    p95_ttfb = models.IntegerField(default=0, help_text='首字返回 P95')

    # --- 缓存命中延迟（单独统计，通常 <50ms）---
    cache_hit_p50_latency = models.IntegerField(default=0,
                                                  help_text='缓存命中请求 P50 延迟')
    cache_hit_p95_latency = models.IntegerField(default=0,
                                                  help_text='缓存命中请求 P95 延迟')

    # --- 比率指标 ---
    cache_hit_rate = models.FloatField(default=0.0, help_text='缓存命中率')
    llm_success_rate = models.FloatField(default=0.0,
                                          help_text='LLM 成功率（仅统计非缓存请求）')
    llm_timeout_rate = models.FloatField(default=0.0,
                                          help_text='LLM 超时率')
    embedding_error_rate = models.FloatField(default=0.0,
                                              help_text='Embedding 错误率')
    avg_tokens_per_second = models.FloatField(default=0.0,
                                               help_text='Token 生成速率（仅非缓存请求）')

    # --- Token & 成本（仅非缓存请求）---
    total_tokens_prompt = models.BigIntegerField(default=0)
    total_tokens_completion = models.BigIntegerField(default=0)
    total_cost = models.DecimalField(max_digits=12, decimal_places=6, default=0)

    # --- 延迟直方图（JSON，按 100ms 分桶，仅非缓存请求）---
    latency_histogram = models.JSONField(default=dict, blank=True,
                                          help_text='{区间: 计数} 示例: {"0-100": 123, "100-200": 456}')

    # --- 错误分布 ---
    error_distribution = models.JSONField(default=dict, blank=True,
                                           help_text='{错误类型: 计数} 示例: {"timeout": 5, "network": 2}')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'analytics_system_metrics_report'
        indexes = [
            models.Index(fields=['-report_date'], name='idx_smr_date'),
        ]


class OrgUsageReport(models.Model):
    """组织维度使用报表（Celery Beat 预计算 + UPSERT 一致性）

    - 使用 update_or_create 实现 UPSERT，重复执行任务不会产生重复数据
    - 支持 部门汇总（team_id=-1）和 团队明细（team_id=具体值）两种粒度
      注意：使用 -1 哨兵值而非 NULL，因为 PostgreSQL unique_together
      中 NULL != NULL，会导致 update_or_create(team_id=None) 每次都 INSERT
    - 普通用户的 Token/费用从 QaRecord 的 user 字段反查，确保数据溯源
    """

    id = models.BigAutoField(primary_key=True)
    report_date = models.DateField()

    # 组织维度
    department_id = models.IntegerField(null=True, blank=True)
    department_name = models.CharField(max_length=64, default='')
    # -1 表示部门级汇总（含所有子团队），>=0 表示具体团队 ID
    team_id = models.IntegerField(default=-1,
                                   help_text='-1=部门级汇总（含所有子团队），>=0=具体团队 ID')
    team_name = models.CharField(max_length=64, default='')

    # --- 核心指标 ---
    qa_count = models.IntegerField(default=0)
    user_count = models.IntegerField(default=0, help_text='当日活跃用户数（distinct）')
    total_tokens = models.BigIntegerField(default=0)
    total_cost = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    avg_latency_ms = models.IntegerField(default=0)
    p95_latency_ms = models.IntegerField(default=0,
                                          help_text='该组织请求的 P95 延迟（Python 排序计算）')
    good_feedback_rate = models.FloatField(default=0.0,
                                            help_text='好评率（有反馈的请求中）')

    # --- 缓存命中指标（组织维度细分）---
    cache_hit_count = models.IntegerField(default=0)
    cache_hit_rate = models.FloatField(default=0.0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'analytics_org_usage_report'
        indexes = [
            models.Index(fields=['-report_date']),
            models.Index(fields=['department_id', 'team_id']),
        ]
        # (date, dept, team_id) 唯一约束，team_id 使用 -1 哨兵值
        # 保证 update_or_create UPSERT 能正确匹配
        unique_together = [('report_date', 'department_id', 'team_id')]


class QueueDepthLog(models.Model):
    """Celery 队列深度定时快照（每 5 分钟，存 PG 用于历史趋势展示）

    - 每 5 分钟通过 Redis LLEN 直接查询 Celery 队列长度（O(1) 操作）
    - 同时记录 Worker 数量，便于评估 Worker 配置是否合理
    - 存 PG 而非纯 Redis：Dashboard 需展示历史趋势（如最近 7 天队列积压变化），
      Redis 仅保留 7 天历史，PG 可保留更长时间供运维分析
    - minute_bucket: 精确到分钟的时间桶，配合 unique_together 防止重复写入
      （Beat 任务重入或 Worker 重启可能导致同一分钟执行两次）
    """

    id = models.BigAutoField(primary_key=True)
    queue_name = models.CharField(max_length=64,
                                 help_text='队列名: default / parse / memory / email / analytics')
    depth = models.IntegerField(default=0,
                                 help_text='待处理任务数（Redis LLEN 查询结果）')
    worker_count = models.IntegerField(default=0,
                                        help_text='活跃 Worker 数（Celery inspect API 获取）')
    avg_wait_sec = models.FloatField(default=0.0,
                                      help_text='队列任务平均等待时间（估算值，后续可扩展）')
    task_types = models.JSONField(default=list,
                                   help_text='队列中各类型任务计数（后续扩展）')

    created_at = models.DateTimeField(auto_now_add=True)
    # minute_bucket 用于唯一约束，将 created_at 截断到分钟粒度
    minute_bucket = models.DateTimeField(null=True, blank=True,
                                         help_text='快照时间截断到分钟（用于唯一约束防重复）')

    class Meta:
        db_table = 'analytics_queue_depth_log'
        indexes = [
            models.Index(fields=['queue_name', '-created_at'], name='idx_qdl_queue_time'),
            models.Index(fields=['-created_at'], name='idx_qdl_time'),
        ]
        # 同一队列在同一分钟内只能有一条记录，防止 Beat 重入/Worker 重启产生重复数据
        unique_together = [('queue_name', 'minute_bucket')]


class AnswerQualityReport(models.Model):
    """回答忠实度评估报告（Celery Beat 异步评估，使用便宜模型）

    - 仅评估 is_hit_cache=False 且 is_success=True 的 QaRecord
      （缓存命中答案在首次生成时已评估，避免重复消耗 Token）
    - 使用便宜模型（deepseek-chat）批量评估，每小时最多 N 条（.env 可配置），
      同时控制每日成本上限，防止意外高额消费
    - status 字段跟踪评估状态，失败时可重试
    """

    STATUS_CHOICES = [
        ('pending', '待评估'),
        ('completed', '已完成'),
        ('failed', '失败（可重试）'),
    ]

    id = models.BigAutoField(primary_key=True)
    qa_record = models.OneToOneField(QaRecord, on_delete=models.CASCADE,
                                     db_column='qa_record_id',
                                     related_name='quality_report')

    # --- 评估结果 ---
    faithfulness_score = models.FloatField(default=0.0,
                                            help_text='忠实度 0-1（分数越高表示回答越忠实于原文）')
    faithfulness_reason = models.TextField(default='',
                                            help_text='评估理由（中文，便于运营直接阅读）')

    # --- 评估元数据 ---
    eval_model = models.CharField(max_length=64, help_text='使用的评估模型')
    eval_tokens_used = models.IntegerField(default=0)
    eval_cost = models.DecimalField(max_digits=8, decimal_places=6, default=0,
                                     help_text='本次评估消耗费用（元）')
    eval_latency_ms = models.IntegerField(default=0)

    # --- 状态机 ---
    status = models.CharField(max_length=16, default='pending', choices=STATUS_CHOICES)
    error_message = models.TextField(default='', blank=True)
    retry_count = models.IntegerField(default=0, help_text='失败重试次数')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'analytics_answer_quality_report'
        indexes = [
            models.Index(fields=['-created_at'], name='idx_aqr_time'),
            models.Index(fields=['status', '-created_at'], name='idx_aqr_status_time'),
            models.Index(fields=['faithfulness_score'], name='idx_aqr_score'),
        ]
