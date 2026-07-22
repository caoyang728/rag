"""
memory app - 四层记忆 Model
对齐数据库设计 D 域（D1/D2/D3/D4）
四层记忆架构
    D1 Session         - 会话主表
    D2 SessionMemory   - 会话记忆（一句话摘要 + 关键实体）
    D3 UserMemory      - 用户长期偏好（单例：每用户一条）
    D4 GlobalMemory    - 全局知识/公司规则
    + 短时记忆走 Redis（不入库）
"""
from django.db import models
from django.contrib.postgres.fields import ArrayField


class Session(models.Model):
    """D1 session - 会话主表"""

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey('users.SysUser', on_delete=models.CASCADE,
                             db_column='user_id', related_name='sessions')
    title = models.CharField(max_length=256, default='新会话')
    root_type = models.CharField(max_length=32, default='company_doc',
                                  help_text='会话默认的知识库根类型')
    is_archived = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    last_active_at = models.DateTimeField(auto_now=True)
    turn_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'memory_session'
        indexes = [
            models.Index(fields=['user', '-last_active_at'], name='idx_sess_user_act'),
            models.Index(fields=['is_archived'], name='idx_sess_archived'),
        ]

    def __str__(self):
        return f'Sess<{self.id}>{self.title}'


class SessionMemory(models.Model):
    """D2 session_memory - 会话级摘要记忆
    每 N 轮触发一次提炼，写入 summary + entities"""

    id = models.BigAutoField(primary_key=True)
    session = models.OneToOneField(Session, on_delete=models.CASCADE,
                                    db_column='session_id', related_name='memory')
    summary = models.TextField(default='', help_text='会话主题一句话摘要')
    entities = ArrayField(models.CharField(max_length=64), default=list, blank=True,
                          help_text='关键实体：人名/产品名/项目名')
    keywords = ArrayField(models.CharField(max_length=32), default=list, blank=True)
    turn_refined = models.IntegerField(default=0, help_text='上次提炼时的轮次')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'memory_session_memory'


class UserMemory(models.Model):
    """D3 user_memory - 用户长期记忆（单例：每用户一条）
    存储用户偏好、常用查询模式、专业领域"""

    id = models.BigAutoField(primary_key=True)
    user = models.OneToOneField('users.SysUser', on_delete=models.CASCADE,
                                 db_column='user_id', related_name='memory')
    preferences = models.JSONField(default=dict, blank=True,
                                    help_text='偏好：{tone:"专业", length:"简洁", ...}')
    domain_tags = ArrayField(models.CharField(max_length=32), default=list, blank=True,
                              help_text='用户擅长/关心的领域')
    frequent_topics = ArrayField(models.CharField(max_length=64), default=list, blank=True,
                                  help_text='高频问的主题')
    profile_text = models.TextField(default='', help_text='拼装成 Prompt 用的画像文本')
    session_refined_count = models.IntegerField(default=0,
                                                 help_text='被提炼过多少次会话')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'memory_user_memory'


class GlobalMemory(models.Model):
    """D4 global_memory - 全局记忆（公司级知识/规则）
    scope_root_types 限定作用域"""

    SCOPE_CHOICES = [
        ('all', 'all'),
        ('company_doc', 'company_doc'),
        ('code_kb', 'code_kb'),
        ('general_reasoning', 'general_reasoning'),
        ('ops_fault', 'ops_fault'),
    ]

    id = models.BigAutoField(primary_key=True)
    key = models.CharField(max_length=64, unique=True, help_text='记忆键，如 "company_rules"')
    content = models.TextField(help_text='记忆内容，直接拼进 System Prompt')
    scope_root_types = ArrayField(models.CharField(max_length=32, choices=SCOPE_CHOICES),
                                   default=list, blank=True,
                                   help_text='作用于哪些根类型；空表示 all')
    priority = models.IntegerField(default=0, help_text='数值越大优先级越高')
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'memory_global_memory'
        indexes = [
            models.Index(fields=['is_enabled', '-priority'], name='idx_gm_enable_pri'),
        ]

    def __str__(self):
        return f'GM<{self.key}>'
