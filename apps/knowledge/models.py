"""
knowledge app - 节点 & 文档 & 分块 & 代码块 & 图片 Model
对齐数据库设计 B 域（B1/B2/B3/B4/B6）+ C 域（C1）
树形节点+visibility 三级可见+file_hash 唯一防重复+图片双存储模式
"""
import uuid as uuid_lib
from django.db import models
from django.contrib.postgres.fields import ArrayField


class KnowledgeNode(models.Model):
    """B1 knowledge_node - 树形节点
    层级：1=知识库(kb) / 2=部门(dept) / 3=团队(team) / 4+=业务分类(category, 支持无限子级)
    权限仅到 team 级（node_level≤3），业务分类（node_level≥4）仅做分类用途
    """

    NODE_TYPE_CHOICES = [
        ('root', 'root'),
        ('folder', 'folder'),
        ('leaf', 'leaf'),
    ]

    id = models.BigAutoField(primary_key=True)
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE,
                               db_column='parent_id', related_name='children')
    root_type = models.CharField(max_length=32)
    node_type = models.CharField(max_length=16, choices=NODE_TYPE_CHOICES, default='folder')
    node_level = models.SmallIntegerField(default=4,
                                           help_text='节点层级: 1=kb 2=dept 3=team 4+=业务分类（≥4 支持无限子级，仅团队组长可管理）')
    name = models.CharField(max_length=128)
    path = models.CharField(max_length=512, default='/',
                             help_text='冗余路径 /kb_id/dept_id/team_id/cat_id/...（支持无限级业务分类）')
    depth = models.SmallIntegerField(default=0)
    description = models.TextField(blank=True, default='')
    order_no = models.IntegerField(default=0)
    is_deleted = models.BooleanField(default=False)
    ref_id = models.BigIntegerField(null=True, blank=True,
                                     help_text='关联源对象 ID：node_level=2 存 dept.id，node_level=3 存 team.id')
    created_by = models.ForeignKey('users.User', null=True, blank=True,
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
        db_table = 'knowledge_node_permission'
        unique_together = [('node', 'subject_type', 'subject_id', 'action')]
        indexes = [
            models.Index(fields=['subject_type', 'subject_id'], name='idx_npo_subject'),
        ]


class Document(models.Model):
    """B3 document - 文档元数据
    visible_scope 三档: team(仅归属团队) / dept(归属全部门) / public(全公司)
    所有文档归属团队节点，上传自动填充 kb/dept/team，仅 category 手动选
    """

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
    VISIBLE_SCOPE_CHOICES = [
        ('team', '仅归属团队'),
        ('dept', '归属全部门'),
        ('public', '全公司公开'),
    ]
    AUDIT_STATUS_CHOICES = [
        ('pending_team', '待团队组长一审'),
        ('pending_compliance', '待合规二审'),
        ('rejected', '审核驳回'),
        ('passed', '双审通过'),
        ('archived', '归档'),
        ('deleted', '逻辑删除'),
    ]
    SECRET_LEVEL_CHOICES = [
        (1, '普通'),
        (2, '内部'),
        (3, '机密'),
        (4, '绝密'),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid_lib.uuid4, editable=False, unique=True)
    # 固定4层节点归属
    kb_node_id = models.BigIntegerField(null=True, blank=True, help_text='一级知识库节点ID')
    dept_node_id = models.BigIntegerField(null=True, blank=True, help_text='二级部门节点ID（归属，不可变）')
    team_node_id = models.BigIntegerField(null=True, blank=True, help_text='三级团队节点ID（归属，不可变）')
    category_node_id = models.BigIntegerField(null=True, blank=True, help_text='四级业务分类节点ID')
    node = models.ForeignKey(KnowledgeNode, on_delete=models.PROTECT, db_column='node_id',
                             related_name='documents', help_text='关联节点（category_node）')
    title = models.CharField(max_length=256)
    file_name = models.CharField(max_length=256)
    file_type = models.CharField(max_length=16, choices=FILE_TYPE_CHOICES)
    file_size = models.BigIntegerField(default=0)
    file_hash = models.CharField(max_length=64,
                                  help_text='sha256 文件内容哈希，用于版本识别')
    file_path = models.CharField(max_length=512, blank=True, default='',
                                 help_text='本地存储路径或 OSS URL')
    mime_type = models.CharField(max_length=64, blank=True, default='')
    # 权限冗余字段
    owner = models.ForeignKey('users.User', on_delete=models.PROTECT, db_column='owner_id',
                              related_name='owned_documents')
    owner_team_id = models.BigIntegerField(null=True, blank=True,
                                            help_text='上传者所在团队快照，防止团队变动导致权限漂移')
    visible_scope = models.CharField(max_length=16, choices=VISIBLE_SCOPE_CHOICES, default='team')
    secret_level = models.SmallIntegerField(choices=SECRET_LEVEL_CHOICES, default=1,
                                             help_text='密级 1普通~4绝密，4禁止public')
    audit_status = models.CharField(max_length=32, choices=AUDIT_STATUS_CHOICES, default='pending_team')
    has_deny_user = models.BooleanField(default=False, help_text='是否有黑名单用户（加速检索跳过黑名单查询）')
    has_cross_team = models.BooleanField(default=False, help_text='是否有跨团队授权（加速检索跳过跨团队查询）')
    has_allow_user = models.BooleanField(default=False, help_text='是否有个人白名单（加速检索跳过白名单查询）')
    allow_download = models.BooleanField(default=False, help_text='是否允许下载该文档')
    allow_share = models.BooleanField(default=False, help_text='是否允许分享该文档')
    root_type = models.CharField(max_length=32, help_text='冗余：根节点类型，加速检索过滤')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='pending',
                               help_text='处理状态: pending/parsing/.../done/failed')
    error_message = models.TextField(blank=True, default='')
    chunk_count = models.IntegerField(default=0)
    version = models.IntegerField(default=1)
    version_tag = models.CharField(max_length=64, default='', blank=True,
                                    help_text='版本标签，如 v1.0、v2.1，用于区分同一文件的不同版本')
    tags = ArrayField(models.CharField(max_length=32), default=list, blank=True)
    extra = models.JSONField(default=dict, blank=True)
    is_deleted = models.BooleanField(default=False)
    delete_time = models.DateTimeField(null=True, blank=True, help_text='逻辑删除时间')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    restored_at = models.DateTimeField(null=True, blank=True, help_text='恢复时间（用于审计追溯）')
    restored_by = models.ForeignKey('users.User', null=True, blank=True,
                                    on_delete=models.SET_NULL, db_column='restored_by',
                                    related_name='restored_documents',
                                    help_text='恢复人')

    class Meta:
        db_table = 'knowledge_document'
        indexes = [
            models.Index(fields=['kb_node_id'], name='idx_doc_kb_node'),
            models.Index(fields=['dept_node_id'], name='idx_doc_dept_node'),
            models.Index(fields=['team_node_id'], name='idx_doc_team_node'),
            models.Index(fields=['node'], name='idx_doc_node'),
            models.Index(fields=['owner'], name='idx_doc_owner'),
            models.Index(fields=['audit_status'], name='idx_doc_audit_status'),
            models.Index(fields=['status'], name='idx_doc_status'),
            models.Index(fields=['visible_scope', 'root_type'], name='idx_doc_visroot'),
            models.Index(fields=['file_hash'], name='idx_doc_hash'),
            models.Index(fields=['node', 'file_name', 'version_tag'], name='idx_doc_node_name_version'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['node', 'file_name', 'version_tag'],
                condition=models.Q(is_deleted=False),
                name='unique_doc_node_name_version',
            ),
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
        db_table = 'knowledge_image'
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


class DocOperationLog(models.Model):
    """知识库操作日志 — 只追加不删不改，完整审计链
    记录文档/节点的敏感操作，用于审计追溯。
    """
    ACTION_CHOICES = [
        # 文档操作
        ('doc_create', '上传文档'),
        ('doc_delete', '删除文档'),
        ('doc_visibility_change', '修改可见范围'),
        ('doc_download', '下载文档'),
        ('doc_reparse', '重新解析'),
        ('doc_restore', '恢复文档'),
        # 节点操作
        ('node_create', '创建节点'),
        ('node_update', '修改节点'),
        ('node_delete', '删除节点'),
        # 审核操作
        ('doc_audit_team_pass', '团队一审通过'),
        ('doc_audit_team_reject', '团队一审驳回'),
        ('doc_audit_compliance_pass', '合规二审通过'),
        ('doc_audit_compliance_reject', '合规二审驳回'),
        # 权限操作
        ('doc_grant', '授权（白名单/跨团队）'),
        ('doc_revoke', '撤销授权'),
        ('doc_grant_expire', '授权到期'),
        ('doc_deny_add', '添加黑名单'),
        ('doc_deny_remove', '移除黑名单'),
        ('doc_archive', '归档文档'),
        ('doc_physical_destroy', '物理销毁'),
    ]

    id = models.BigAutoField(primary_key=True)
    action = models.CharField(max_length=32, choices=ACTION_CHOICES, db_index=True)
    operator = models.ForeignKey('users.User', on_delete=models.SET_NULL,
                                 null=True, blank=True, db_column='operator_id',
                                 related_name='+')
    operator_name = models.CharField(max_length=128, blank=True, default='')
    # 关联目标
    document = models.ForeignKey(Document, on_delete=models.SET_NULL,
                                 null=True, blank=True, db_column='document_id',
                                 related_name='+')
    node = models.ForeignKey(KnowledgeNode, on_delete=models.SET_NULL,
                             null=True, blank=True, db_column='node_id',
                             related_name='+')
    # 详情
    detail = models.JSONField(default=dict, blank=True,
                              help_text='操作详情，如变更前后的值、目标用户等')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'knowledge_doc_operation_log'
        indexes = [
            models.Index(fields=['action', 'created_at']),
            models.Index(fields=['document', '-created_at']),
            models.Index(fields=['node', '-created_at']),
            models.Index(fields=['operator', '-created_at']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'OpLog<{self.id}>{self.action}:{self.operator_name}'
