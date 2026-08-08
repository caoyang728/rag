"""
system app - 系统配置 & Celery 任务日志 & LLM 调用日志 & 导出日志
对齐数据库设计 H1 + G1/G4/G5
"""
from django.db import models


class SystemConfig(models.Model):
    """H1 system_config - 系统配置（KV）

    设计说明：
    - value 统一以 TextField 存储，按 value_type 在读取层做类型转换
    - is_secret=true 的项前端展示掩码，写入时仍以明文落库（当前未做加密），
      后续如需加密可接入 Fernet 字段，不影响外部读取契约
    - is_readonly=true 的项前端不可改（如 EMBEDDING_DIM 改了需重建向量索引），
      这类项只能在 .env 中修改后重启生效
    - category 用于前端分组展示，避免一堆配置堆在一起难以维护
    - risk_level 标记风险等级，高风险项的变更工单需超管复核后才能生效
    """

    VALUE_TYPE_CHOICES = [
        ('string', 'string'),
        ('int', 'int'),
        ('float', 'float'),
        ('bool', 'bool'),
        ('json', 'json'),
    ]

    # 风险等级：高风险项（存储模式/邮件开关/敏感词/登录锁定等）变更影响面大，
    # 工单流程需在普通审批之上叠加超管复核，避免单人误改造成线上故障
    RISK_LEVEL_CHOICES = [
        ('normal', '普通'),
        ('high', '高风险'),
    ]

    # 配置分组：与前端系统配置页 tab 对应
    CATEGORY_CHOICES = [
        ('llm', 'llm'),                      # LLM 模型与调用参数
        ('embedding', 'embedding'),          # Embedding / Rerank
        ('retrieval', 'retrieval'),          # 检索参数
        ('storage', 'storage'),              # 文档/图片/OSS 存储
        ('email', 'email'),                  # 邮件 SMTP
        ('agent', 'agent'),                  # Agent 模式与工具
        ('security', 'security'),            # 敏感词过滤 / 登录锁定
        ('memory', 'memory'),                # 记忆 Token 预算
        ('analytics', 'analytics'),          # Redis DB / 队列监控
        ('eval', 'eval'),                    # 评估与回归测试
    ]

    id = models.BigAutoField(primary_key=True)
    key = models.CharField(max_length=64, unique=True)
    value = models.TextField(default='')
    value_type = models.CharField(max_length=8, choices=VALUE_TYPE_CHOICES, default='string')
    # 中文名（简短），前端作为主标题展示
    label = models.CharField(max_length=64, blank=True, default='', help_text='中文名，前端主标题')
    # 详细说明（选项解释、取值范围等），前端作为副说明展示
    description = models.CharField(max_length=256, blank=True, default='', help_text='详细说明/选项解释')
    # 可选值列表（JSON 数组，如 [{"value":"docker","label":"本地优先"}]），非空时前端渲染 select
    options = models.TextField(blank=True, default='', help_text='可选值列表 JSON，空=自由输入')
    # 单位（如 秒/分钟/MB/元/次），前端在 input 后面显示
    unit = models.CharField(max_length=8, blank=True, default='', help_text='单位，前端在控件后显示')
    is_secret = models.BooleanField(default=False, help_text='加密存储的敏感项')
    # 前端分组：按业务域归类，避免一坨配置无序展示
    category = models.CharField(max_length=16, choices=CATEGORY_CHOICES, default='llm',
                                 help_text='配置分组，用于前端按 tab 展示')
    # 只读标记：EMBEDDING_DIM/AGENT_DEFAULT_MODE 等改了影响索引或路由的项，
    # 不允许前端直接改，必须改 .env 后重启
    is_readonly = models.BooleanField(default=False, help_text='只读项，前端不可改，需改 .env')
    # 风险等级：高风险项需走"审核 + 超管复核"流程，普通项仅需审核
    risk_level = models.CharField(max_length=8, choices=RISK_LEVEL_CHOICES, default='normal',
                                   help_text='风险等级，高风险项需超管复核')
    updated_by = models.ForeignKey('users.User', null=True, blank=True,
                                    on_delete=models.SET_NULL, db_column='updated_by')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'system_config'
        indexes = [
            models.Index(fields=['category'], name='idx_sc_category'),
        ]

    def __str__(self):
        return f'Cfg<{self.key}>'


class LLMModel(models.Model):
    """LLM/Embedding/Rerank 模型配置

    """
    MODEL_TYPE_CHOICES = [
        ('llm', 'LLM 对话模型'),
        ('embedding', 'Embedding 向量模型'),
        ('rerank', 'Rerank 重排序模型'),
    ]

    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=64, verbose_name='显示名称')
    provider = models.CharField(max_length=32, verbose_name='提供商', help_text='如 deepseek、openai')
    model_type = models.CharField(max_length=16, choices=MODEL_TYPE_CHOICES, verbose_name='模型类型')
    base_url = models.URLField(max_length=256, blank=True, default='', verbose_name='API 地址')
    model_name = models.CharField(max_length=128, verbose_name='模型标识', help_text='如 deepseek-chat')
    # 模型级超时（秒）：为空时回退到 SystemConfig.LLM_TIMEOUT，再回退到 settings
    # 推理模型（deepseek-reasoner）通常需要更长超时，可在此单独设置
    timeout = models.IntegerField(null=True, blank=True, verbose_name='超时秒数',
                                   help_text='为空时回退到全局 LLM_TIMEOUT')
    is_active = models.BooleanField(default=True, verbose_name='启用')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'system_llm_model'
        verbose_name = '模型配置'
        verbose_name_plural = verbose_name
        ordering = ['model_type', 'name']
        # 同一类型下显示名称不重复，防止运维误建重复条目导致前端选择混乱
        unique_together = [('model_type', 'name')]

    def __str__(self):
        return f'[{self.model_type}] {self.name} ({self.model_name})'

# ============================================================================
# 工单相关模型已统一迁移至 apps.users.TicketList / TicketPermissionDetail，
# 配置/模型/定时任务工单（config/model/schedule）复用主表 detail JSON。
# system 侧旧模型 ConfigChangeTicket / ModelChangeTicket / Ticket 已废弃删除。
# ============================================================================


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
