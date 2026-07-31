"""
analytics app - 关键词权重 & 准确率日报 & 性能监控 & RAG 质量评估 Model
对齐数据库设计 E3/E4 + 系统监控域（SystemMetricsReport/OrgUsageReport/QueueDepthLog/AnswerQualityReport）
KeywordWeight 支持基于历史准确率的动态关键词加权
SystemMetricsReport: P50/P95/P99、缓存命中率、LLM 错误率等系统级日报
OrgUsageReport: 部门/团队维度的对话次数、Token 消耗、费用统计
QueueDepthLog: Celery 队列深度定时快照（每 5 分钟），用于 Dashboard 展示历史趋势
AnswerQualityReport: 回答忠实度评估报告（异步 LLM 评估）
GoldenDataset/GoldenQuestion/GoldenRelevantDoc/GoldenReferenceAnswer: 离线评估黄金测试集
MultiDimensionScore: 多维度回答质量评估（Faithfulness/Relevance/Completeness/Correctness/Harmlessness/ContextRecall）
DocumentQualityReport: 文档入库质量报告（解析/切分/向量化质量量化）
RetrievalQualityReport: 检索质量报告（Recall@K/MRR/NDCG + 各阶段增益分析）
CoverageReport: 知识库覆盖率报告（热门问题覆盖、知识空白检测）
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


# ============================================================================
# 黄金测试集（离线评估用）
# ============================================================================

class GoldenDataset(models.Model):
    """黄金测试集 - 离线评估的标注数据集

    用于离线评估检索和回答质量。管理员可创建多个测试集，
    每个测试集包含一批业务典型问题，每个问题标注相关文档和参考答案。
    """
    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('active', '已启用'),
        ('archived', '已归档'),
    ]

    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=128, help_text='测试集名称，如"HR领域2026Q3"')
    description = models.TextField(default='', blank=True)
    root_type = models.CharField(max_length=32, default='company_doc',
                                 help_text='测试集覆盖的知识库类型')
    status = models.CharField(max_length=16, default='active', choices=STATUS_CHOICES)
    question_count = models.IntegerField(default=0, help_text='问题数量（冗余，批量更新）')
    version = models.CharField(max_length=16, default='v1', help_text='版本号，如 v1/v2')
    created_by = models.ForeignKey('users.User', on_delete=models.SET_NULL,
                                    null=True, db_column='created_by',
                                    related_name='golden_datasets')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'analytics_golden_dataset'
        indexes = [
            models.Index(fields=['status', '-updated_at'], name='idx_gds_status_time'),
            models.Index(fields=['root_type'], name='idx_gds_root'),
        ]


class GoldenQuestion(models.Model):
    """黄金测试问题 - 测试集中的单个问题

    每个问题可关联多个相关文档（通过 GoldenRelevantDoc）和一个参考答案。
    """
    id = models.BigAutoField(primary_key=True)
    dataset = models.ForeignKey(GoldenDataset, on_delete=models.CASCADE,
                                 db_column='dataset_id', related_name='questions')
    question = models.TextField(help_text='测试问题')
    question_type = models.CharField(max_length=16, default='factoid',
                                      help_text='问题类型: factoid(事实型)/reasoning(推理型)/summary(摘要型)/procedural(操作型)')
    difficulty = models.CharField(max_length=8, default='medium',
                                  help_text='难度: easy/medium/hard')
    tags = ArrayField(models.CharField(max_length=32), default=list, blank=True,
                      help_text='标签，如 ["HR","入职"]')
    order = models.IntegerField(default=0, help_text='排序号，用于在测试集中排列')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'analytics_golden_question'
        indexes = [
            models.Index(fields=['dataset_id', 'order'], name='idx_gq_dataset_order'),
        ]


class GoldenRelevantDoc(models.Model):
    """黄金测试相关文档标注 - 标注问题相关的文档

    每个问题可关联多个文档，并用 relevance_level 标注相关程度。
    用于计算检索 Recall@K 指标。
    """
    RELEVANCE_CHOICES = [
        ('high', '高度相关'),
        ('medium', '相关'),
        ('low', '弱相关'),
    ]

    id = models.BigAutoField(primary_key=True)
    question = models.ForeignKey(GoldenQuestion, on_delete=models.CASCADE,
                                  db_column='question_id', related_name='relevant_docs')
    document = models.ForeignKey('knowledge.Document', on_delete=models.CASCADE,
                                  db_column='document_id', related_name='golden_annotations')
    chunk_id = models.BigIntegerField(null=True, blank=True,
                                       help_text='具体 chunk ID（可选，精确到切片级别）')
    relevance_level = models.CharField(max_length=8, default='medium', choices=RELEVANCE_CHOICES)
    note = models.CharField(max_length=256, default='', blank=True, help_text='标注备注')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'analytics_golden_relevant_doc'
        indexes = [
            models.Index(fields=['question_id'], name='idx_grd_question'),
            models.Index(fields=['document_id'], name='idx_grd_document'),
        ]


class GoldenReferenceAnswer(models.Model):
    """黄金测试参考答案 - 问题的标准答案

    用于评估回答的正确性（Correctness）和完整性（Completeness）。
    """
    id = models.BigAutoField(primary_key=True)
    question = models.OneToOneField(GoldenQuestion, on_delete=models.CASCADE,
                                     db_column='question_id', related_name='reference_answer')
    reference_answer = models.TextField(help_text='参考答案文本')
    reference_answer_source = models.CharField(max_length=64, default='manual',
                                                help_text='答案来源: manual(人工编写)/llm(LLM生成后人工审核)')
    key_points = models.JSONField(default=list, blank=True,
                                   help_text='答案关键点列表，用于 Completeness 评估')
    created_by = models.ForeignKey('users.User', on_delete=models.SET_NULL,
                                    null=True, db_column='created_by')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'analytics_golden_reference_answer'


# ============================================================================
# 多维度评估引擎
# ============================================================================

class MultiDimensionScore(models.Model):
    """多维度回答质量评估 - 对 QaRecord 进行 6 维度评估

    评估维度:
    - faithfulness: 忠实度（回答是否基于 context，无幻觉）
    - relevance: 相关性（回答是否切中问题要害）
    - completeness: 完整性（回答是否覆盖了 context 中的关键点）
    - correctness: 正确性（回答是否存在事实错误）
    - harmlessness: 无害性（回答是否安全合规）
    - context_recall: 上下文召回率（context 是否包含回答所需信息）
    """
    DIMENSION_CHOICES = [
        ('faithfulness', '忠实度'),
        ('relevance', '相关性'),
        ('completeness', '完整性'),
        ('correctness', '正确性'),
        ('harmlessness', '无害性'),
        ('context_recall', '上下文召回率'),
    ]
    STATUS_CHOICES = [
        ('pending', '待评估'),
        ('completed', '已完成'),
        ('failed', '失败'),
    ]

    id = models.BigAutoField(primary_key=True)
    qa_record = models.ForeignKey(QaRecord, on_delete=models.CASCADE,
                                   db_column='qa_record_id', related_name='multi_dim_scores')
    dimension = models.CharField(max_length=20, choices=DIMENSION_CHOICES)
    score = models.FloatField(default=0.0, help_text='0-1 分')
    reason = models.TextField(default='', blank=True, help_text='评估理由')

    # 原子级事实核查结果（JSON）
    atomic_facts = models.JSONField(default=list, blank=True,
                                     help_text='[{"fact": "...", "supported": true/false, "reason": "..."}]')

    # 评估元数据
    eval_model = models.CharField(max_length=64, default='deepseek-chat')
    eval_tokens_used = models.IntegerField(default=0)
    eval_cost = models.DecimalField(max_digits=8, decimal_places=6, default=0)
    eval_latency_ms = models.IntegerField(default=0)
    retry_count = models.IntegerField(default=0)

    # 关联离线评估批次
    eval_batch_id = models.CharField(max_length=64, default='', blank=True,
                                      help_text='离线评估批次 ID，关联到 RetrievalQualityReport 等')

    status = models.CharField(max_length=16, default='pending', choices=STATUS_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'analytics_multi_dimension_score'
        unique_together = [('qa_record_id', 'dimension')]
        indexes = [
            models.Index(fields=['qa_record_id'], name='idx_mds_qa'),
            models.Index(fields=['dimension', '-score'], name='idx_mds_dim_score'),
            models.Index(fields=['status', '-created_at'], name='idx_mds_status_time'),
        ]


# ============================================================================
# 文档质量报告
# ============================================================================

class DocumentQualityReport(models.Model):
    """文档入库质量报告 - 量化评估每个文档的解析/切分/向量化质量

    指标:
    - parse_success_rate: 解析成功率
    - text_extraction_rate: 文本提取完整率
    - table_preservation_rate: 表格保留率
    - chunk_count: 切片总数
    - avg_chunk_size: 平均切片大小（字符数）
    - embedding_coverage: 向量化覆盖率
    - quality_score: 综合质量评分 0-100
    """
    id = models.BigAutoField(primary_key=True)
    document = models.OneToOneField('knowledge.Document', on_delete=models.CASCADE,
                                      db_column='document_id', related_name='quality_report')

    # --- 解析质量 ---
    parse_status = models.CharField(max_length=16, default='pending',
                                     help_text='解析状态: success/failed/partial')
    parse_error_rate = models.FloatField(default=0.0,
                                           help_text='解析错误率（无法提取的内容占比）')
    text_extraction_chars = models.IntegerField(default=0, help_text='提取的文本字符数')
    expected_chars = models.IntegerField(default=0, help_text='预期字符数（估算值）')
    text_extraction_rate = models.FloatField(default=0.0,
                                              help_text='文本提取完整率 = actual/expected')

    # --- 切分质量 ---
    chunk_count = models.IntegerField(default=0)
    avg_chunk_chars = models.IntegerField(default=0, help_text='平均切片字符数')
    chunk_size_stddev = models.FloatField(default=0.0, help_text='切片大小标准差')
    min_chunk_chars = models.IntegerField(default=0)
    max_chunk_chars = models.IntegerField(default=0)
    table_chunk_count = models.IntegerField(default=0, help_text='表格类型切片数')

    # --- 向量化质量 ---
    embedding_success_rate = models.FloatField(default=0.0,
                                                help_text='向量化成功率')
    failed_chunk_count = models.IntegerField(default=0,
                                              help_text='向量化失败的 chunk 数')

    # --- 综合评分 ---
    quality_score = models.FloatField(default=0.0,
                                       help_text='综合质量评分 0-100（解析*0.4 + 切分*0.3 + 向量化*0.3）')
    quality_issues = models.JSONField(default=list, blank=True,
                                       help_text='发现的问题列表: [{"level": "warning", "type": "too_short", "detail": "..."}]')

    evaluated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'analytics_document_quality_report'
        indexes = [
            models.Index(fields=['quality_score'], name='idx_dqr_score'),
            models.Index(fields=['-created_at'], name='idx_dqr_time'),
        ]


# ============================================================================
# 检索质量报告
# ============================================================================

class RetrievalQualityReport(models.Model):
    """检索质量报告 - 离线评估检索质量指标

    指标:
    - recall_at_k: 召回率（K=5/10/20）
    - mrr: 平均倒数排名
    - ndcg_at_k: 归一化折损累积增益
    - vector_gain: 向量检索贡献的增益
    - bm25_gain: BM25 贡献的增益
    - rerank_gain: Rerank 贡献的增益
    """
    id = models.BigAutoField(primary_key=True)
    dataset = models.ForeignKey(GoldenDataset, on_delete=models.CASCADE,
                                 db_column='dataset_id', related_name='retrieval_reports')
    eval_batch_id = models.CharField(max_length=64, default='',
                                      help_text='评估批次 ID，用于关联同一批评估的所有指标')

    # --- 核心指标 ---
    recall_at_5 = models.FloatField(default=0.0)
    recall_at_10 = models.FloatField(default=0.0)
    recall_at_20 = models.FloatField(default=0.0)
    mrr = models.FloatField(default=0.0, help_text='Mean Reciprocal Rank')
    ndcg_at_5 = models.FloatField(default=0.0)
    ndcg_at_10 = models.FloatField(default=0.0)

    # --- 各阶段增益分析 ---
    vector_recall_at_10 = models.FloatField(default=0.0, help_text='纯向量检索 Recall@10')
    bm25_recall_at_10 = models.FloatField(default=0.0, help_text='纯 BM25 检索 Recall@10')
    hybrid_recall_at_10 = models.FloatField(default=0.0, help_text='混合检索 Recall@10')
    rerank_recall_at_10 = models.FloatField(default=0.0, help_text='Rerank 后 Recall@10')

    # --- 统计数据 ---
    total_questions = models.IntegerField(default=0)
    questions_with_hits = models.IntegerField(default=0,
                                                help_text='至少命中一个相关文档的问题数')
    questions_without_hits = models.IntegerField(default=0,
                                                  help_text='未命中任何相关文档的问题数')
    avg_latency_ms = models.IntegerField(default=0)

    # --- 配置快照 ---
    config_snapshot = models.JSONField(default=dict, blank=True,
                                        help_text='评估时的检索参数配置快照')

    status = models.CharField(max_length=16, default='pending',
                               choices=[('pending', '待评估'), ('completed', '已完成'), ('failed', '失败')])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'analytics_retrieval_quality_report'
        indexes = [
            models.Index(fields=['dataset_id', '-created_at'], name='idx_rqr_dataset_time'),
            models.Index(fields=['-created_at'], name='idx_rqr_time'),
        ]


# ============================================================================
# 知识库覆盖率报告
# ============================================================================

class CoverageReport(models.Model):
    """知识库覆盖率报告 - 评估知识库的覆盖范围和盲区

    指标:
    - hot_query_coverage: 热门问题覆盖率
    - knowledge_gap_queries: 知识空白查询（长期无相关文档的查询）
    - duplicate_chunk_rate: 重复切片率
    - domain_coverage: 各领域/部门的知识覆盖
    """
    id = models.BigAutoField(primary_key=True)
    report_date = models.DateField()

    # --- 热门问题覆盖 ---
    total_hot_queries = models.IntegerField(default=0,
                                             help_text='统计周期内热门查询数')
    covered_queries = models.IntegerField(default=0,
                                          help_text='有相关文档命中的查询数')
    uncovered_queries = models.IntegerField(default=0,
                                             help_text='无任何相关文档的查询数')
    hot_query_coverage_rate = models.FloatField(default=0.0,
                                                help_text='热门问题覆盖率 = covered/total')

    # --- 知识空白 ---
    gap_queries = models.JSONField(default=list, blank=True,
                                    help_text='[[{"query": "...", "count": 5, "suggestion": "建议补充XXX"}]')
    gap_count = models.IntegerField(default=0, help_text='知识空白查询数量')

    # --- 重复检测 ---
    duplicate_chunk_rate = models.FloatField(default=0.0,
                                              help_text='重复切片率（近似重复的 chunk 占比）')
    duplicate_chunk_count = models.IntegerField(default=0)

    # --- 领域覆盖 ---
    domain_coverage = models.JSONField(default=dict, blank=True,
                                        help_text='{"domain_name": {"doc_count": 10, "chunk_count": 50, "query_hit_rate": 0.85}}')

    # --- 反馈闭环 ---
    feedback_loop_count = models.IntegerField(default=0,
                                               help_text='已自动关联到反馈的问题数')
    feedback_resolved_count = models.IntegerField(default=0,
                                                   help_text='已通过新增文档/修复解决的反馈数')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'analytics_coverage_report'
        indexes = [
            models.Index(fields=['-report_date'], name='idx_covr_date'),
        ]
