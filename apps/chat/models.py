"""
chat app - 问答记录 & 反馈 & 热点缓存 & 任务拆分 Model
对齐数据库设计 E 域（E1/E2/E5/E6）
QaRecord 全链路日志（检索命中/命中片段/耗时/Token/成本） + 热点缓存加速
"""
import uuid as uuid_lib
from django.db import models
from django.contrib.postgres.fields import ArrayField

from apps.memory.models import Session


class QaRecord(models.Model):
    """E1 qa_record - 问答主记录（一次问答一行）
    全链路可观测
    - retrieval_hits：命中的 chunk_id 数组，可回溯
    - latency_*：分阶段耗时
    - tokens_*：Prompt/Completion 分别统计
    - cost_estimate：按官方定价估算成本
    """

    ROLE_CHOICES = [
        ('user', 'user'),
        ('assistant', 'assistant'),
    ]
    ANSWER_TYPE_CHOICES = [
        ('rag', 'rag'),
        ('reasoning', 'reasoning'),
        ('mixed', 'mixed'),
        ('refused', 'refused'),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid_lib.uuid4, editable=False, unique=True)
    session = models.ForeignKey(Session, on_delete=models.CASCADE,
                                 db_column='session_id', related_name='qa_records')
    user = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True,
                              db_column='user_id', related_name='qa_records')
    turn_index = models.IntegerField(default=0, help_text='会话内轮次')

    # 用户问
    question = models.TextField(help_text='用户原始问题')
    question_tokens = models.IntegerField(default=0)

    # 检索
    retrieval_hits = ArrayField(models.BigIntegerField(), default=list, blank=True,
                                 help_text='命中的 document_chunk.id')
    retrieval_scores = models.JSONField(default=list, blank=True,
                                         help_text='[{chunk_id,vector,bm25,rrf,rerank}]')
    root_type = models.CharField(max_length=32, default='company_doc',
                                  help_text='本次问答的根类型（选定的知识库）')

    # 助手回答
    answer = models.TextField(default='')
    answer_type = models.CharField(max_length=16, choices=ANSWER_TYPE_CHOICES, default='rag')
    answer_tokens = models.IntegerField(default=0)
    citations = models.JSONField(default=list, blank=True,
                                  help_text='[{chunk_id,doc_title,section,page}]')

    # 观测指标
    latency_retrieval_ms = models.IntegerField(default=0)
    latency_rerank_ms = models.IntegerField(default=0)
    latency_llm_ms = models.IntegerField(default=0)
    latency_total_ms = models.IntegerField(default=0)
    latency_ttfb_ms = models.IntegerField(default=0, help_text='首字返回耗时（请求起点→首个 delta，ms）')
    tokens_prompt = models.IntegerField(default=0)
    tokens_completion = models.IntegerField(default=0)
    cost_estimate = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    llm_provider = models.CharField(max_length=32, default='deepseek')
    llm_model = models.CharField(max_length=64, default='deepseek-chat')

    # 状态
    is_hit_cache = models.BooleanField(default=False, help_text='是否命中热点缓存')
    is_task_split = models.BooleanField(default=False, help_text='是否触发复杂任务拆分')
    error_message = models.TextField(blank=True, default='')

    # --- 路由来源（LLM Wiki / GraphRAG / RAG 三层路由）---
    # 记录本次问答的路由决策链路，用于评估与监控各层命中率/置信度/延迟对比
    route_source = models.CharField(max_length=32, null=True, blank=True,
                                    help_text='路由来源: wiki/graphrag_local/graphrag_global/rag/agent')
    route_trace = models.JSONField(null=True, blank=True,
                                   help_text='路由决策追踪日志（每层置信度与耗时）')

    # --- 错误类型分类（结构化统计 LLM/Embedding 失败原因）---
    # 缓存命中时 error_type 为空字符串；LLM 调用失败时分类记录，
    # 用于统计 timeout_rate、rate_limit_rate、embedding_error_rate 等细分指标
    ERROR_TYPE_CHOICES = [
        ('timeout', 'timeout'),
        ('rate_limit', 'rate_limit'),
        ('network', 'network'),
        ('content_filter', 'content_filter'),
        ('server_error', 'server_error'),
        ('embedding_error', 'embedding_error'),
        ('unknown', 'unknown'),
    ]
    error_type = models.CharField(max_length=32, choices=ERROR_TYPE_CHOICES,
                                  default='', blank=True,
                                  help_text='错误类型：timeout/rate_limit/network/content_filter/server_error/embedding_error')
    # is_success=False 表示对话链路中断（含 LLM 错误、Embedding 失败等），
    # 区别于 answer_type='refused'（正常的"无相关资料"拒答）
    is_success = models.BooleanField(default=True,
                                      help_text='对话是否成功完成（False=链路中断）')

    # --- 内容安全审查标记 ---
    # is_filtered=True 表示 LLM 输出命中敏感词被拦截/脱敏（区别于链路错误的 is_success）
    # - block 动作：流式中断，answer 字段保存已生成的部分内容（审计用，前端不展示）
    # - mask 动作：answer 字段保存脱敏后的内容（与前端展示一致）
    # filter_reason 记录命中的敏感词分类，便于运营分析高频违规类型
    is_filtered = models.BooleanField(default=False,
                                       help_text='是否命中敏感词审查（block/mask）')
    filter_reason = models.CharField(max_length=128, blank=True, default='',
                                      help_text='命中原因：敏感词分类或命中词列表（审计用）')

    # --- Token 生成速率 ---
    # 保存时计算 completion_tokens / (latency_llm_ms / 1000)，
    # 避免 Dashboard 端重复计算；缓存命中时为 0
    tokens_per_second = models.FloatField(default=0.0,
                                           help_text='生成速率: completion_tokens / llm_duration_sec')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'chat_qa_record'
        indexes = [
            models.Index(fields=['session', 'turn_index'], name='idx_qa_sess_turn'),
            models.Index(fields=['user', '-created_at'], name='idx_qa_user_time'),
            models.Index(fields=['root_type'], name='idx_qa_root'),
            models.Index(fields=['answer_type'], name='idx_qa_ans_type'),
            models.Index(fields=['-created_at'], name='idx_qa_created'),
            # 加速失败率和错误分类统计
            models.Index(fields=['is_success', '-created_at'], name='idx_qa_success_time'),
            models.Index(fields=['error_type', '-created_at'], name='idx_qa_errtype_time'),
        ]

    def __str__(self):
        return f'QA<{self.id}>{self.question[:20]}'


class QaFeedback(models.Model):
    """E2 qa_feedback - 用户反馈"""

    RATING_CHOICES = [
        (-1, '差评'),
        (0, '中性'),
        (1, '好评'),
    ]
    STATUS_CHOICES = [
        ('pending', 'pending'),
        ('processing', 'processing'),
        ('resolved', 'resolved'),
        ('ignored', 'ignored'),
    ]

    id = models.BigAutoField(primary_key=True)
    qa_record = models.OneToOneField(QaRecord, on_delete=models.CASCADE,
                                      db_column='qa_id', related_name='feedback')
    user = models.ForeignKey('users.User', on_delete=models.CASCADE,
                              db_column='user_id', related_name='feedbacks')
    rating = models.SmallIntegerField(default=0, help_text='1好评 -1差评 0中性')
    tags = ArrayField(models.CharField(max_length=32), default=list, blank=True,
                      help_text='差评原因标签：不准确/无引用/回答慢...')
    comment = models.TextField(blank=True, default='')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='pending')
    admin_reply = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'chat_feedback'
        indexes = [
            models.Index(fields=['status', '-created_at'], name='idx_fb_status_time'),
            models.Index(fields=['rating'], name='idx_fb_rating'),
            models.Index(fields=['-created_at'], name='idx_fb_created'),
        ]


class HotQaCache(models.Model):
    """E5 hot_qa_cache - 热点问答缓存
    question_hash + root_type + visibility_scope 三键定位，命中率高"""

    id = models.BigAutoField(primary_key=True)
    question_hash = models.CharField(max_length=64, help_text='sha256(normalized_question)')
    root_type = models.CharField(max_length=32)
    visibility_scope = models.CharField(max_length=64, default='public',
                                         help_text='缓存权限组：public / org_d3_t7（组织分组）')
    question = models.TextField()
    answer = models.TextField()
    citations = models.JSONField(default=list, blank=True)
    cited_doc_ids = ArrayField(models.BigIntegerField(), default=list, blank=True,
                                help_text='回答引用的文档 ID 列表，用于缓存命中后权限校验')
    hit_count = models.IntegerField(default=0)
    last_hit_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'chat_hot_qa_cache'
        unique_together = [('question_hash', 'root_type', 'visibility_scope')]
        indexes = [
            models.Index(fields=['-hit_count'], name='idx_hot_hits'),
        ]


class TaskDecomposition(models.Model):
    """E6 task_decomposition - 复杂任务拆分记录
    LLM 输出 JSON 子任务列表；每个子任务独立检索+回答，最后合并"""

    STATUS_CHOICES = [
        ('planning', 'planning'),
        ('executing', 'executing'),
        ('merging', 'merging'),
        ('done', 'done'),
        ('failed', 'failed'),
    ]

    id = models.BigAutoField(primary_key=True)
    qa_record = models.OneToOneField(QaRecord, on_delete=models.CASCADE,
                                      db_column='qa_id', related_name='decomposition')
    original_question = models.TextField()
    sub_tasks = models.JSONField(default=list, help_text='[{index,question,dep,answer}]')
    merged_answer = models.TextField(blank=True, default='')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='planning')
    total_latency_ms = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'chat_task_decomposition'
