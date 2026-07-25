"""
system app - 系统配置 & Celery 任务日志 & LLM 调用日志 & 导出日志
对齐数据库设计 H1 + G1/G4/G5
"""
from django.db import models


class SystemConfig(models.Model):
    """H1 system_config - 系统配置（KV）"""

    VALUE_TYPE_CHOICES = [
        ('string', 'string'),
        ('int', 'int'),
        ('float', 'float'),
        ('bool', 'bool'),
        ('json', 'json'),
    ]

    id = models.BigAutoField(primary_key=True)
    key = models.CharField(max_length=64, unique=True)
    value = models.TextField(default='')
    value_type = models.CharField(max_length=8, choices=VALUE_TYPE_CHOICES, default='string')
    description = models.CharField(max_length=256, blank=True, default='')
    is_secret = models.BooleanField(default=False, help_text='加密存储的敏感项')
    updated_by = models.ForeignKey('users.User', null=True, blank=True,
                                    on_delete=models.SET_NULL, db_column='updated_by')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'system_config'

    def __str__(self):
        return f'Cfg<{self.key}>'


class CeleryTaskLog(models.Model):
    """G1 celery_task_log - 异步任务日志"""

    STATUS_CHOICES = [
        ('pending', 'pending'),
        ('started', 'started'),
        ('success', 'success'),
        ('failure', 'failure'),
        ('retry', 'retry'),
        ('revoked', 'revoked'),
    ]

    id = models.BigAutoField(primary_key=True)
    task_id = models.CharField(max_length=64, unique=True)
    task_name = models.CharField(max_length=128)
    queue = models.CharField(max_length=32, default='default')
    args = models.JSONField(default=list, blank=True)
    kwargs = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='pending')
    result = models.TextField(blank=True, default='')
    error_message = models.TextField(blank=True, default='')
    retry_count = models.IntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'system_celery_task_log'
        indexes = [
            models.Index(fields=['status', '-created_at'], name='idx_ctl_status'),
            models.Index(fields=['task_name'], name='idx_ctl_name'),
        ]


class LlmCallLog(models.Model):
    """G4 llm_call_log - LLM 调用日志（成本可观测）"""

    id = models.BigAutoField(primary_key=True)
    provider = models.CharField(max_length=32, default='deepseek')
    model = models.CharField(max_length=64, default='deepseek-chat')
    scene = models.CharField(max_length=32, default='qa',
                              help_text='qa/task_split/memory_refine/embedding/rerank')
    user = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True,
                              blank=True, db_column='user_id')
    prompt_tokens = models.IntegerField(default=0)
    completion_tokens = models.IntegerField(default=0)
    total_tokens = models.IntegerField(default=0)
    cost = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    latency_ms = models.IntegerField(default=0)
    status = models.CharField(max_length=16, default='success')
    error_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'system_llm_call_log'
        indexes = [
            models.Index(fields=['provider', 'model', '-created_at'], name='idx_llm_pm_time'),
            models.Index(fields=['scene'], name='idx_llm_scene'),
        ]


class DataExportLog(models.Model):
    """G5 data_export_log - 数据导出日志（审计+防越权）"""

    STATUS_CHOICES = [
        ('pending', 'pending'),
        ('running', 'running'),
        ('success', 'success'),
        ('failed', 'failed'),
    ]

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True,
                              db_column='user_id')
    export_type = models.CharField(max_length=32,
                                    help_text='qa_records/audit/users/documents')
    query_params = models.JSONField(default=dict, blank=True)
    file_path = models.CharField(max_length=512, blank=True, default='')
    file_size = models.BigIntegerField(default=0)
    row_count = models.IntegerField(default=0)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='pending')
    error_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'system_data_export_log'
        indexes = [
            models.Index(fields=['user', '-created_at'], name='idx_del_user_time'),
        ]
