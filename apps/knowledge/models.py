"""
knowledge app - 节点 & 文档 & 分块 & 代码块 & 图片 Model
对齐数据库设计 B 域（B1/B2/B3/B4/B6）+ C 域（C1）
树形节点+visibility 三级可见+file_hash 唯一防重复+图片双存储模式
"""
import uuid as uuid_lib
from django.db import models
from django.contrib.postgres.fields import ArrayField


class KnowledgeNode(models.Model):
    """B1 knowledge_node - 树形节点（根/中间/叶子）"""

    NODE_TYPE_CHOICES = [
        ('root', 'root'),
        ('folder', 'folder'),
        ('leaf', 'leaf'),
    ]

    id = models.BigAutoField(primary_key=True)
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE,
                               db_column='parent_id', related_name='children')
    root_type = models.CharField(max_length=32)

    @classmethod
    def get_root_types(cls):
        """动态获取所有根类型（从数据库查询）"""
        # 获取所有唯一的 root_type，以及每个 root_type 对应的第一个节点名称
        root_types = {}
        for node in cls.objects.filter(
            node_type='root', is_deleted=False
        ).order_by('id'):
            if node.root_type not in root_types:
                root_types[node.root_type] = node.name
        return list(root_types.items())

    @classmethod
    def get_root_type_choices(cls):
        """获取根类型选择列表（用于表单）"""
        return cls.get_root_types()

    node_type = models.CharField(max_length=16, choices=NODE_TYPE_CHOICES, default='folder')
    name = models.CharField(max_length=128)
    # path 冗余存 "/1/3/5/" 便于递归权限判定
    path = models.CharField(max_length=512, default='/')
    depth = models.SmallIntegerField(default=0)
    description = models.TextField(blank=True, default='')
    order_no = models.IntegerField(default=0)
    is_deleted = models.BooleanField(default=False)
    created_by = models.ForeignKey('users.SysUser', null=True, blank=True,
                                    on_delete=models.SET_NULL, db_column='created_by',
                                    related_name='created_nodes')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'knowledge_node'
        indexes = [
            models.Index(fields=['parent'], name='idx_kn_parent'),
            models.Index(fields=['root_type'], name='idx_kn_root_type'),
            models.Index(fields=['path'], name='idx_kn_path'),
        ]

    def __str__(self):
        return f'{self.root_type}:{self.name}'


class NodePermissionOverride(models.Model):
    """B2 node_permission_override - 节点级权限覆盖（可选，MVP 简化）"""

    SUBJECT_TYPE_CHOICES = [
        ('user', 'user'),
        ('team', 'team'),
        ('role', 'role'),
    ]
    ACTION_CHOICES = [
        ('read', 'read'),
        ('write', 'write'),
        ('admin', 'admin'),
    ]

    id = models.BigAutoField(primary_key=True)
    node = models.ForeignKey(KnowledgeNode, on_delete=models.CASCADE, db_column='node_id',
                             related_name='permission_overrides')
    subject_type = models.CharField(max_length=16, choices=SUBJECT_TYPE_CHOICES)
    subject_id = models.BigIntegerField()
    action = models.CharField(max_length=16, choices=ACTION_CHOICES, default='read')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'knowledge_node_permission_override'
        unique_together = [('node', 'subject_type', 'subject_id', 'action')]
        indexes = [
            models.Index(fields=['subject_type', 'subject_id'], name='idx_npo_subject'),
        ]


class Document(models.Model):
    """B3 document - 文档元数据
    visibility 三级可见（1私有/2团队/3公开/4系统级） + file_hash 唯一防重复上传
    owner_team_id 快照防止团队变动导致的权限泄露"""

    STATUS_CHOICES = [
        ('pending', 'pending'),
        ('parsing', 'parsing'),
        ('desensitizing', 'desensitizing'),
        ('chunking', 'chunking'),
        ('embedding', 'embedding'),
        ('embedding_failed', 'embedding_failed'),
        ('done', 'done'),
        ('failed', 'failed'),
    ]
    FILE_TYPE_CHOICES = [
        ('pdf', 'pdf'),
        ('docx', 'docx'),
        ('markdown', 'markdown'),
        ('txt', 'txt'),
        ('code', 'code'),
        ('config', 'config'),
        ('other', 'other'),
    ]
    VISIBILITY_CHOICES = [
        (1, '私有'),
        (2, '部门'),
        (3, '团队'),
        (4, '公开'),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid_lib.uuid4, editable=False, unique=True)
    node = models.ForeignKey(KnowledgeNode, on_delete=models.PROTECT, db_column='node_id',
                             related_name='documents')
    title = models.CharField(max_length=256)
    file_name = models.CharField(max_length=256)
    file_type = models.CharField(max_length=16, choices=FILE_TYPE_CHOICES)
    file_size = models.BigIntegerField(default=0)
    file_hash = models.CharField(max_length=64, unique=True,
                                  help_text='sha256 文件内容哈希，防重复上传')
    file_path = models.CharField(max_length=512, blank=True, default='',
                                 help_text='本地存储路径或 OSS URL')
    mime_type = models.CharField(max_length=64, blank=True, default='')
    # ⭐ 权限冗余字段：便于检索时 SQL 一次过滤
    owner = models.ForeignKey('users.SysUser', on_delete=models.PROTECT, db_column='owner_id',
                              related_name='owned_documents')
    owner_team_id = models.BigIntegerField(null=True, blank=True,
                                            help_text='上传者所在团队快照，防止团队变动导致权限漂移')
    visibility = models.SmallIntegerField(choices=VISIBILITY_CHOICES, default=1)
    root_type = models.CharField(max_length=32, help_text='冗余：根节点类型，加速检索过滤')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='pending')
    error_message = models.TextField(blank=True, default='')
    chunk_count = models.IntegerField(default=0)
    version = models.IntegerField(default=1)
    tags = ArrayField(models.CharField(max_length=32), default=list, blank=True)
    extra = models.JSONField(default=dict, blank=True)
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # 恢复审计字段
    restored_at = models.DateTimeField(null=True, blank=True,
                                        help_text='恢复时间（用于审计追溯）')
    restored_by = models.ForeignKey('users.SysUser', null=True, blank=True,
                                    on_delete=models.SET_NULL, db_column='restored_by',
                                    related_name='restored_documents',
                                    help_text='恢复人')

    class Meta:
        db_table = 'knowledge_document'
        indexes = [
            models.Index(fields=['node'], name='idx_doc_node'),
            models.Index(fields=['owner'], name='idx_doc_owner'),
            models.Index(fields=['status'], name='idx_doc_status'),
            models.Index(fields=['visibility', 'root_type'], name='idx_doc_visroot'),
            models.Index(fields=['file_hash'], name='idx_doc_hash'),
        ]

    def __str__(self):
        return f'Doc<{self.id}>{self.title}'


class DocumentChunk(models.Model):
    """B4 document_chunk - 文档切片
    chunk_type 区分正文/表格/图片/代码；section_path 存章节路径便于溯源"""

    CHUNK_TYPE_CHOICES = [
        ('text', 'text'),
        ('table', 'table'),
        ('image', 'image'),
        ('code', 'code'),
        ('config', 'config'),
    ]

    id = models.BigAutoField(primary_key=True)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, db_column='document_id',
                                  related_name='chunks')
    chunk_index = models.IntegerField(help_text='文档内切片序号，从 0 开始')
    chunk_type = models.CharField(max_length=16, choices=CHUNK_TYPE_CHOICES, default='text')
    content = models.TextField(help_text='切片原文')
    content_length = models.IntegerField(default=0)
    section_path = models.CharField(max_length=512, blank=True, default='',
                                     help_text='如 "第一章>1.1 节>表 1-2"')
    page_number = models.IntegerField(null=True, blank=True)
    image_id = models.BigIntegerField(null=True, blank=True,
                                       help_text='若 chunk_type=image，关联 image_resource.id')
    extra = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'knowledge_document_chunk'
        unique_together = [('document', 'chunk_index')]
        indexes = [
            models.Index(fields=['document'], name='idx_chunk_doc'),
            models.Index(fields=['chunk_type'], name='idx_chunk_type'),
        ]

    def __str__(self):
        return f'Chunk<{self.document_id}#{self.chunk_index}>'


class CodeChunk(models.Model):
    """B6 code_chunk - 代码专用切片（AST 解析结果）
    按 函数/类/方法 切片，保留 signature/params 便于精准召回"""

    SYMBOL_TYPE_CHOICES = [
        ('function', 'function'),
        ('class', 'class'),
        ('method', 'method'),
        ('module', 'module'),
        ('variable', 'variable'),
    ]

    id = models.BigAutoField(primary_key=True)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, db_column='document_id',
                                  related_name='code_chunks')
    chunk = models.ForeignKey(DocumentChunk, on_delete=models.CASCADE,
                              db_column='chunk_id', related_name='code_meta')
    language = models.CharField(max_length=32, default='python')
    symbol_type = models.CharField(max_length=16, choices=SYMBOL_TYPE_CHOICES)
    symbol_name = models.CharField(max_length=128)
    signature = models.TextField(blank=True, default='', help_text='函数签名，如 def foo(a:int)->str')
    params = models.JSONField(default=list, blank=True, help_text='参数列表 [{name,type,default}]')
    docstring = models.TextField(blank=True, default='')
    start_line = models.IntegerField(default=0)
    end_line = models.IntegerField(default=0)
    parent_symbol = models.CharField(max_length=128, blank=True, default='',
                                      help_text='所属类名，用于方法')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'knowledge_code_chunk'
        indexes = [
            models.Index(fields=['document'], name='idx_cc_doc'),
            models.Index(fields=['symbol_name'], name='idx_cc_symbol'),
            models.Index(fields=['language', 'symbol_type'], name='idx_cc_lang_type'),
        ]

    def __str__(self):
        return f'{self.language}:{self.symbol_type}:{self.symbol_name}'


class ImageResource(models.Model):
    """C1 image_resource - 图片资源
    双存储模式（base64 开发 / oss 生产），CHECK 约束保证一致性"""

    STORAGE_MODE_CHOICES = [
        ('base64', 'base64'),
        ('oss', 'oss'),
    ]

    id = models.BigAutoField(primary_key=True)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, db_column='document_id',
                                  related_name='images', null=True, blank=True)
    storage_mode = models.CharField(max_length=8, choices=STORAGE_MODE_CHOICES, default='base64')
    base64_data = models.TextField(blank=True, default='',
                                    help_text='当 storage_mode=base64 时存储')
    oss_url = models.CharField(max_length=512, blank=True, default='',
                                help_text='当 storage_mode=oss 时存储')
    mime_type = models.CharField(max_length=32, default='image/png')
    width = models.IntegerField(default=0)
    height = models.IntegerField(default=0)
    size_bytes = models.IntegerField(default=0)
    ocr_text = models.TextField(blank=True, default='', help_text='OCR 结果，便于图片检索')
    caption = models.TextField(blank=True, default='', help_text='图片说明/图注')
    page_number = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'knowledge_image_resource'
        indexes = [
            models.Index(fields=['document'], name='idx_img_doc'),
            models.Index(fields=['storage_mode'], name='idx_img_mode'),
        ]
        # CHECK 约束：base64 模式必须有 base64_data，oss 模式必须有 oss_url
        constraints = [
            models.CheckConstraint(
                check=(
                    (models.Q(storage_mode='base64') & ~models.Q(base64_data=''))
                    | (models.Q(storage_mode='oss') & ~models.Q(oss_url=''))
                ),
                name='ck_image_storage_consistency',
            ),
        ]

    def __str__(self):
        return f'Image<{self.id}>{self.storage_mode}'
