"""
analytics app - 关键词权重 & 准确率日报 & 性能监控 & RAG 质量评估 Model
对齐数据库设计 E3/E4 + 系统监控域（SystemMetricsReport/OrgUsageReport/QueueDepthLog）
KeywordWeight 支持基于历史准确率的动态关键词加权
SystemMetricsReport: P50/P95/P99、缓存命中率、LLM 错误率等系统级日报
OrgUsageReport: 部门/团队维度的对话次数、Token 消耗、费用统计
QueueDepthLog: Celery 队列深度定时快照（每 5 分钟），用于 Dashboard 展示历史趋势
GoldenDataset/GoldenQuestion/GoldenRelevantDoc/GoldenReferenceAnswer: 离线评估黄金测试集
MultiDimensionScore: 多维度回答质量评估（DeepEval 12 维 + 历史兼容维度）
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
    p99_latency_llm = models.IntegerField(default=0, help_text='LLM 调用 P99')
    p50_latency_retrieval = models.IntegerField(default=0, help_text='检索 P50')
    p95_latency_retrieval = models.IntegerField(default=0, help_text='检索 P95')
    p99_latency_retrieval = models.IntegerField(default=0, help_text='检索 P99')
    p50_ttfb = models.IntegerField(default=0, help_text='首字返回 P50')
    p95_ttfb = models.IntegerField(default=0, help_text='首字返回 P95')
    p99_ttfb = models.IntegerField(default=0, help_text='首字返回 P99')

    # --- 缓存命中延迟（单独统计，通常 <50ms）---
    cache_hit_p50_latency = models.IntegerField(default=0, help_text='缓存命中请求 P50 延迟')
    cache_hit_p95_latency = models.IntegerField(default=0, help_text='缓存命中请求 P95 延迟')
    cache_hit_p99_latency = models.IntegerField(default=0, help_text='缓存命中请求 P99 延迟')

    # --- 比率指标 ---
    cache_hit_rate = models.FloatField(default=0.0, help_text='缓存命中率')
    llm_success_rate = models.FloatField(default=0.0, help_text='LLM 成功率（仅统计非缓存请求）')
    llm_timeout_rate = models.FloatField(default=0.0, help_text='LLM 超时率')
    embedding_error_rate = models.FloatField(default=0.0, help_text='Embedding 错误率')
    avg_tokens_per_second = models.FloatField(default=0.0, help_text='Token 生成速率（仅非缓存请求）')

    # --- Token & 成本（仅非缓存请求）---
    total_tokens_prompt = models.BigIntegerField(default=0)
    total_tokens_completion = models.BigIntegerField(default=0)
    total_cost = models.DecimalField(max_digits=12, decimal_places=6, default=0)

    # --- 延迟直方图（JSON，按 100ms 分桶，仅非缓存请求）---
    latency_histogram = models.JSONField(default=dict, blank=True, help_text='{区间: 计数} 示例: {"0-100": 123, "100-200": 456}')

    # --- 错误分布 ---
    error_distribution = models.JSONField(default=dict, blank=True, help_text='{错误类型: 计数} 示例: {"timeout": 5, "network": 2}')

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


# ============================================================================
# 黄金测试集（离线评估用）
# ============================================================================

class GoldenDataset(models.Model):
    """黄金测试集 - 离线评估的标注数据集

    用于离线评估检索和回答质量。管理员可创建多个测试集，
    每个测试集包含一批业务典型问题，每个问题标注相关文档和参考答案。

    dataset_type 区分测试集来源:
    - custom: 人工维护的静态测试集,长期稳定,用于回归基线
    - regression_low_score: 低分回归测试集,从生产低分对话沉淀,
      连续通过 N 次后人工 review 移除,防止已知 bad case 退化
    """
    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('active', '已启用'),
        ('archived', '已归档'),
    ]
    DATASET_TYPE_CHOICES = [
        ('custom', '自定义'),
        ('regression_low_score', '低分回归'),
    ]

    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=128, help_text='测试集名称，如"HR领域2026Q3"')
    description = models.TextField(default='', blank=True)
    root_type = models.CharField(max_length=32, default='company_doc',
                                 help_text='测试集覆盖的领域')
    status = models.CharField(max_length=16, default='active', choices=STATUS_CHOICES)
    # dataset_type 用于区分人工维护的静态测试集与自动沉淀的低分回归测试集
    # 默认 custom 保持向后兼容:历史数据无需迁移即归入自定义类型
    dataset_type = models.CharField(
        max_length=32, default='custom', choices=DATASET_TYPE_CHOICES,
        help_text='测试集类型: custom(人工维护) / regression_low_score(低分回归)')
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
            # 低分回归测试集按类型查询是高频操作(沉淀/评估/前端列表)
            models.Index(fields=['dataset_type', '-updated_at'], name='idx_gds_type_time'),
        ]


class GoldenQuestion(models.Model):
    """黄金测试问题 - 测试集中的单个问题

    每个问题可关联多个相关文档（通过 GoldenRelevantDoc）和一个参考答案。

    低分回归测试集专用字段(自定义测试集保持默认值不使用):
    - source_qa_record_id: 沉淀来源的 QaRecord.id,用于追溯原始低分对话
    - pass_count: 连续通过次数,每次回归评估通过 +1,失败重置为 0
      达到 suggest_remove_passes(默认 3) 时前端提示"建议人工 review 移除"
    - last_eval_at: 最近一次回归评估时间,用于排序与展示新鲜度
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
    # 低分回归专用:沉淀来源 QA,自定义测试集保持 null
    source_qa_record_id = models.BigIntegerField(
        null=True, blank=True, help_text='低分回归专用:沉淀来源的 QaRecord.id')
    # 低分回归专用:连续通过次数,失败重置为 0;达到阈值前端提示建议移除
    pass_count = models.IntegerField(
        default=0, help_text='低分回归专用:连续通过次数,失败重置为 0')
    # 低分回归专用:最近一次回归评估时间,用于排序与新鲜度展示
    last_eval_at = models.DateTimeField(
        null=True, blank=True, help_text='低分回归专用:最近一次回归评估时间')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'analytics_golden_question'
        indexes = [
            models.Index(fields=['dataset_id', 'order'], name='idx_gq_dataset_order'),
            # 低分回归:按来源 QA 查重(防止同一低分对话重复沉淀)
            models.Index(fields=['source_qa_record_id'], name='idx_gq_source_qa'),
            # 低分回归:按通过次数筛选"建议移除"的候选
            models.Index(fields=['pass_count'], name='idx_gq_pass_count'),
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
    """多维度回答质量评估 - 对 QaRecord 进行多维度评估

    评估维度分四大类(共 12 维,生产评估用 DeepEval,部署前离线评估用 Ragas):
    - 检索质量(1维): context_relevancy 上下文相关性
    - 答案质量(6维): faithfulness 忠实度 / hallucination 幻觉 /
                    answer_relevancy 相关性 / completeness 完整性 /
                    conciseness 简洁性 / clarity 清晰度
    - 安全性(2维): toxicity 毒性 / bias 偏见
    - 业务体验(3维): professionalism 专业性 / helpfulness 有用性 /
                    actionability 可操作性

    历史维度(自研引擎/早期版本,保留兼容):
    - relevance/correctness/harmlessness/context_recall: 自研引擎使用的旧维度名
    """
    DIMENSION_CHOICES = [
        # DeepEval 生产评估 12 维
        ('faithfulness', '忠实度'),
        ('answer_relevancy', '相关性'),
        ('context_relevancy', '上下文相关性'),
        ('hallucination', '幻觉'),
        ('toxicity', '毒性'),
        ('bias', '偏见'),
        ('completeness', '完整性'),
        ('conciseness', '简洁性'),
        ('clarity', '清晰度'),
        ('professionalism', '专业性'),
        ('helpfulness', '有用性'),
        ('actionability', '可操作性'),
        # 自研引擎历史维度(兼容)
        ('relevance', '相关性(自研)'),
        ('correctness', '正确性(自研)'),
        ('harmlessness', '无害性(自研)'),
        ('context_recall', '上下文召回率(自研)'),
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


# ============================================================================
# 低分归因分析
# ============================================================================

class LowScoreAnalysis(models.Model):
    """低分对话归因分析 - 对低分 QA 自动归因 + 给出优化建议

    触发时机:MultiDimensionScore 落库后,若 QA 均分 < threshold 则异步派发归因。
    与 MultiDimensionScore 一对一:同 QA 重新评估后会重新归因(update_or_create 覆盖)。

    归因分类(root_cause_category)结合 RAG 链路 + 12 维评估:
    - retrieval_recall:  检索召回不足(TopK 命中少)
    - retrieval_rank:     检索排序失效(rerank 后相关片段掉出)
    - content_gap:        知识盲区(无对应文档)
    - content_quality:    内容/切片质量差
    - generation_hallucination: 生成幻觉(faithfulness 低 + 检索好)
    - generation_offtopic:      生成跑题(answer_relevancy 低 + 有 contexts)
    - generation_incomplete:    生成不完整(completeness 低)
    - generation_format:        生成表达差(clarity/conciseness 低)
    - safety:            安全问题(toxicity/bias 低,需立即告警)
    - question_side:     问题侧(模糊/超纲,非系统问题)
    - unknown:           无法归因(规则未覆盖,留待人工)

    建议生成策略(分层,控成本):
    - safety: 不走 LLM,直接告警建议(建议无意义,要立即人工处置)
    - 关键维度低分(faithfulness/context_relevancy/answer_relevancy/hallucination): 走 LLM 生成针对性建议
    - 边缘维度低分(conciseness/clarity/professionalism 等): 仅模板建议
    - 多维(>=3)同时低分: 走 LLM(综合问题需深度分析)
    """
    CATEGORY_CHOICES = [
        ('retrieval_recall', '检索召回不足'),
        ('retrieval_rank', '检索排序失效'),
        ('content_gap', '知识盲区'),
        ('content_quality', '内容质量差'),
        ('generation_hallucination', '生成幻觉'),
        ('generation_offtopic', '生成跑题'),
        ('generation_incomplete', '生成不完整'),
        ('generation_format', '生成表达差'),
        ('safety', '安全问题'),
        ('question_side', '问题侧'),
        ('unknown', '无法归因'),
    ]
    LAYER_CHOICES = [
        ('retrieval', '检索层'),
        ('content', '内容层'),
        ('generation', '生成层'),
        ('safety', '安全层'),
        ('system', '系统层'),
        ('question', '问题侧'),
        ('unknown', '未知'),
    ]
    METHOD_CHOICES = [
        ('rule', '规则归因'),
        ('llm', 'LLM 归因'),
        ('hybrid', '规则+LLM'),
    ]
    STATUS_CHOICES = [
        ('pending', '待分析'),
        ('completed', '已完成'),
        ('failed', '失败'),
    ]

    id = models.BigAutoField(primary_key=True)
    qa_record = models.OneToOneField(QaRecord, on_delete=models.CASCADE,
                                      db_column='qa_record_id', related_name='low_score_analysis')
    # 触发归因的均分阈值(快照,便于回溯当时配置)
    avg_score = models.FloatField(default=0.0, help_text='触发归因时的 QA 12 维均分')
    threshold = models.FloatField(default=0.5, help_text='触发归因的阈值')

    # 归因结论
    root_cause_category = models.CharField(max_length=32, choices=CATEGORY_CHOICES,
                                            default='unknown')
    root_cause_detail = models.TextField(default='', blank=True,
                                          help_text='具体原因描述(规则命中条件 / LLM 诊断)')
    affected_layer = models.CharField(max_length=16, choices=LAYER_CHOICES, default='unknown')

    # 低分维度快照(便于前端展示该 QA 哪几个维度拖了后腿)
    low_dimensions = models.JSONField(default=list, blank=True,
                                       help_text='[{"dimension":"faithfulness","score":0.32,"reason":"..."}]')

    # 优化建议
    diagnosis = models.TextField(default='', blank=True,
                                  help_text='一句话诊断(LLM 生成,模板归因为空)')
    suggestions = models.JSONField(default=list, blank=True,
                                    help_text='[{"type":"short_term","action":"..."},{"type":"long_term","action":"..."}]')

    # 归因元数据
    analysis_method = models.CharField(max_length=16, choices=METHOD_CHOICES, default='rule')
    analysis_model = models.CharField(max_length=64, default='', blank=True)
    analysis_tokens_used = models.IntegerField(default=0)
    analysis_cost = models.DecimalField(max_digits=8, decimal_places=6, default=0)
    analysis_latency_ms = models.IntegerField(default=0)

    status = models.CharField(max_length=16, default='pending', choices=STATUS_CHOICES)
    error_message = models.TextField(default='', blank=True, help_text='失败原因(status=failed 时填充)')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'analytics_low_score_analysis'
        indexes = [
            models.Index(fields=['-created_at'], name='idx_lsa_time'),
            models.Index(fields=['root_cause_category', '-created_at'], name='idx_lsa_cat_time'),
            models.Index(fields=['status', '-created_at'], name='idx_lsa_status_time'),
        ]


# ============================================================================
# 三层路由评估（LLM Wiki / GraphRAG / RAG）
# ============================================================================

class RouteAnalysis(models.Model):
    """路由决策分析 - 记录每次路由决策，支撑四层架构的命中率与质量对比

    - route_source: 最终命中的来源（wiki / graphrag_local / graphrag_global / rag）
    - route_trace: 路由链路每层的置信度与耗时快照
    - answer_quality: 与生产评估/反馈关联后的质量分（0-1，12 维均分）
    - qa_record_id: 来源 QaRecord.id（唯一），保证每日聚合任务 update_or_create 幂等
    """

    id = models.BigAutoField(primary_key=True)
    # 来源 QA 记录（唯一键：同一条 QA 只保留一份路由分析，重复聚合不产生脏数据）
    qa_record_id = models.BigIntegerField(unique=True, db_index=True, null=True, blank=True,
                                          help_text='来源 QaRecord.id，聚合幂等键')
    question = models.TextField()
    route_source = models.CharField(max_length=32, db_index=True)
    confidence = models.FloatField(default=0.0)
    route_trace = models.JSONField(default=list, blank=True)
    latency_ms = models.IntegerField(default=0)
    answer_quality = models.FloatField(null=True, blank=True)
    # 来源 QA 的提问时间（看板按天窗口按此过滤；created_at 是聚合时间不可覆盖）
    qa_created_at = models.DateTimeField(null=True, blank=True, db_index=True,
                                         help_text='来源 QaRecord.created_at，看板时间窗口过滤键')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'analytics_route'
        indexes = [
            models.Index(fields=['route_source', 'created_at'], name='idx_ar_source_time'),
            models.Index(fields=['created_at', 'route_source'], name='idx_ar_time_source'),
        ]


class WikiPageQualityScore(models.Model):
    """Wiki 页面质量评估 - 对发布的 WikiPage 按源文档 chunks 做 LLM-as-Judge

    - faithfulness(忠实度): 页面内容是否忠于源文档切片（无幻觉）
    - completeness(完整性): 页面是否完整覆盖源文档的关键要点

    与 MultiDimensionScore 的关系：维度定义与 DeepEval 12 维语义一致，
    但评估对象是 Wiki 页面而非对话，故独立建表（qa_record 关联不适用）。
    """

    DIMENSION_CHOICES = [
        ('faithfulness', '忠实度'),
        ('completeness', '完整性'),
    ]
    STATUS_CHOICES = [
        ('completed', '已完成'),
        ('failed', '失败'),
    ]

    id = models.BigAutoField(primary_key=True)
    page = models.ForeignKey('wiki.WikiPage', on_delete=models.CASCADE,
                             db_column='page_id', related_name='quality_scores')
    dimension = models.CharField(max_length=32, choices=DIMENSION_CHOICES)
    score = models.FloatField(default=0.0, help_text='0-1 分')
    reason = models.TextField(default='', blank=True, help_text='评估理由')
    eval_model = models.CharField(max_length=64, default='deepseek-chat')
    eval_latency_ms = models.IntegerField(default=0)
    status = models.CharField(max_length=16, default='completed', choices=STATUS_CHOICES)
    error_message = models.TextField(default='', blank=True, help_text='失败原因(status=failed 时填充)')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'analytics_wiki_page_quality_score'
        # 同页面同维度只保留一次最新评估（重新评估覆盖）
        unique_together = [('page', 'dimension')]
        indexes = [
            models.Index(fields=['page', 'dimension'], name='idx_wpqs_page_dim'),
            models.Index(fields=['-updated_at'], name='idx_wpqs_time'),
        ]

    def __str__(self):
        return f'WikiScore<{self.page_id}>{self.dimension}={self.score}'


# ============================================================================
# 检索反馈闭环自动化
# ============================================================================

class ChunkClickLog(models.Model):
    """溯源来源点击日志 - 用户点击回答引用卡片时记录

    检索反馈闭环自动化的数据源之一，用于统计"关键词命中 chunk 的点击率"。
    点击发生时前端携带 qa_record_id（本次回答的记录 ID）与 chunk_ids，
    每日聚合任务按 qa_record_id 关联到 QaRecord 的检索命中，归并出点击指标。

    - 点击是低频用户行为，直接 INSERT 即可，无需缓存
    - qa_record_id 允许为空：历史消息卡片点击等无法回填 ID 的场景也能落库
    - 只增不改，作为反馈闭环的原始证据留存
    """

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey('users.User', on_delete=models.CASCADE,
                             null=True, blank=True, db_column='user_id',
                             related_name='chunk_click_logs')
    qa_record = models.ForeignKey('chat.QaRecord', on_delete=models.CASCADE,
                                  null=True, blank=True, db_column='qa_record_id',
                                  related_name='chunk_click_logs',
                                  help_text='被点击回答对应的 QaRecord.id（前端从流式 done 事件回填）')
    document_id = models.BigIntegerField(null=True, blank=True,
                                          help_text='被点击引用对应的文档 ID')
    chunk_id = models.BigIntegerField(help_text='被点击的 chunk id')
    root_type = models.CharField(max_length=32, default='all')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'analytics_chunk_click_log'
        indexes = [
            # 聚合按 qa_record 归并点击，按 chunk 统计，联合索引一次命中
            models.Index(fields=['qa_record_id', 'chunk_id'], name='idx_ccl_qa_chunk'),
            models.Index(fields=['-created_at'], name='idx_ccl_time'),
        ]


class KeywordFeedbackAgg(models.Model):
    """关键词反馈聚合日报 - 检索反馈闭环的每日聚合结果与审计

    每天凌晨聚合前一天数据：统计关键词命中 chunk 的 展示数/点击数/采纳数/负反馈数，
    并按规则计算权重调整量，自动写入 KeywordWeight 或留待人工复核。

    - (report_date, keyword, root_type) 唯一，作为幂等键：同一日期重复执行
      只会更新统计值，不会重复应用权重调整
    - old_score/new_score 记录调整前后权重，配合 status/applied_at 实现权重调整全程可追溯
    - adjust_type 区分 自动(auto) 与 手动(manual)，手动调整记录同样入库，供运营侧统一查看
    """

    ADJUST_TYPE_CHOICES = [
        ('auto', '自动'),
        ('manual', '手动'),
    ]
    STATUS_CHOICES = [
        ('pending', '待复核'),
        ('applied', '已应用'),
        ('ignored', '已忽略'),
    ]

    id = models.BigAutoField(primary_key=True)
    report_date = models.DateField(help_text='聚合日期（业务日期）')
    keyword = models.CharField(max_length=64)
    root_type = models.CharField(max_length=32, default='all')

    # --- 统计指标 ---
    shown_count = models.IntegerField(default=0, help_text='关键词命中 chunk 的展示次数（retrieval_hits）')
    click_count = models.IntegerField(default=0, help_text='展示后被点击的次数')
    adopt_count = models.IntegerField(default=0, help_text='展示并被回答引用的次数（citations 命中）')
    bad_count = models.IntegerField(default=0, help_text='含该关键词的差评对话数')
    click_rate = models.FloatField(default=0.0, help_text='点击率 = click/shown')
    adopt_rate = models.FloatField(default=0.0, help_text='采纳率 = adopt/shown')

    # --- 权重调整 ---
    old_score = models.FloatField(default=1.0, help_text='调整前权重')
    new_score = models.FloatField(default=1.0, help_text='调整后权重（未应用时为目标值）')
    delta = models.FloatField(default=0.0, help_text='本次调整幅度（受单日上限保护）')
    reason = models.CharField(max_length=128, default='', blank=True,
                              help_text='调整原因（规则命中说明，如 采纳率低/点击未采纳/负反馈）')
    adjust_type = models.CharField(max_length=16, choices=ADJUST_TYPE_CHOICES, default='auto')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='pending')

    # --- 审计 ---
    actor = models.ForeignKey('users.User', on_delete=models.SET_NULL,
                              null=True, blank=True, db_column='actor_id',
                              related_name='keyword_feedback_aggs',
                              help_text='操作人（手动调整时必填，自动调整为系统）')
    applied_at = models.DateTimeField(null=True, blank=True, help_text='权重实际生效时间')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'analytics_keyword_feedback_agg'
        # 幂等键：同一日期同一关键词只处理一次，防止每日任务重入导致重复调整
        unique_together = [('report_date', 'keyword', 'root_type')]
        indexes = [
            models.Index(fields=['report_date', 'root_type'], name='idx_kfa_date_root'),
            models.Index(fields=['keyword'], name='idx_kfa_keyword'),
            models.Index(fields=['status', '-created_at'], name='idx_kfa_status_time'),
        ]

    def __str__(self):
        return f'KeywordFeedbackAgg<{self.report_date}>{self.keyword} delta={self.delta:+.2f}'
