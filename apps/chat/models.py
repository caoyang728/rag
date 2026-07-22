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
    user = models.ForeignKey('users.SysUser', on_delete=models.SET_NULL, null=True,
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
    tokens_prompt = models.IntegerField(default=0)
    tokens_completion = models.IntegerField(default=0)
    cost_estimate = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    llm_provider = models.CharField(max_length=32, default='deepseek')
    llm_model = models.CharField(max_length=64, default='deepseek-chat')

    # 状态
    is_hit_cache = models.BooleanField(default=False, help_text='是否命中热点缓存')
    is_task_split = models.BooleanField(default=False, help_text='是否触发复杂任务拆分')
    error_message = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'qa_record'
        indexes = [
            models.Index(fields=['session', 'turn_index'], name='idx_qa_sess_turn'),
            models.Index(fields=['user', '-created_at'], name='idx_qa_user_time'),
            models.Index(fields=['root_type'], name='idx_qa_root'),
            models.Index(fields=['answer_type'], name='idx_qa_ans_type'),
            models.Index(fields=['-created_at'], name='idx_qa_created'),
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
    user = models.ForeignKey('users.SysUser', on_delete=models.CASCADE,
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
        db_table = 'qa_feedback'
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
    visibility_scope = models.CharField(max_length=16, default='public',
                                         help_text='public/team/private')
    question = models.TextField()
    answer = models.TextField()
    citations = models.JSONField(default=list, blank=True)
    hit_count = models.IntegerField(default=0)
    last_hit_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'hot_qa_cache'
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
        db_table = 'task_decomposition'
