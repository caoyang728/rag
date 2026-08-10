"""
knowledge app - 节点 & 文档 & 分块 & 代码块 & 图片 & 资源共享 Model

- KnowledgeNode：节点树（path 路径枚举 + parent 双写），加 owner_user_id
- Document：归属改为 dept_id/team_id 二选一（支持团队或部门归属）+ visibility_level 三档
            + allow_share_request/preview_content/preview_chunks（轻量申请入口）
- ResourceShare：统一资源共享表（单表 + share_scope_type 枚举，支持部门/团队/个人）
                 + resource_type（KNOWLEDGE_BASE/KNOWLEDGE_NODE/DOCUMENT）
                 + inherit_mode（ALL_DESCENDANTS 节点子树继承）
- ResourceBlockList：访问黑名单（仅个人，Deny Override 铁律，节点级继承）
- DocOperationLog：保留，记录文档业务操作（上传/解析/删除/审核）
- PermissionAuditLog：见 apps.users.models，记录权限操作（授权/共享/黑名单）

节点树层级（与 node_sync.py 同步）：
  Level 1: KB root（知识库根节点，自动创建）
  Level 2: 部门节点（folder，ref_id = dept.id）
  Level 3: 团队节点（folder，ref_id = team.id）
  Level 4+: 业务分类节点（组长手动管理，无 ref_id）
"""
import uuid as uuid_lib
from django.db import models
from django.contrib.postgres.fields import ArrayField
from django.utils.translation import gettext_lazy as _


# ============================================================================
# 枚举定义（资源共享与黑名单）
# ============================================================================

class ResourceType(models.TextChoices):
    """资源类型 —— ResourceShare / ResourceBlockList 共用"""
    KNOWLEDGE_BASE = 'KNOWLEDGE_BASE', _('知识库')
    KNOWLEDGE_NODE = 'KNOWLEDGE_NODE', _('知识节点')
    DOCUMENT = 'DOCUMENT', _('文档')


class ShareScopeType(models.TextChoices):
    """共享对象类型 —— 统一表用枚举区分部门/团队/个人，扩展新类型零表改动"""
    DEPT = 'DEPT', _('部门')
    TEAM = 'TEAM', _('团队')
    USER = 'USER', _('个人')


class AccessLevel(models.TextChoices):
    """访问等级 —— 共享授权的权限强度"""
    READ = 'READ', _('只读')
    EDIT = 'EDIT', _('可编辑')


class InheritMode(models.TextChoices):
    """节点级继承模式 —— 仅 resource_type=KNOWLEDGE_NODE 时有意义

    ALL_DESCENDANTS：授权本节点 + 所有子节点 + 子节点下所有文档（默认，前缀匹配 path）
    NODE_ONLY：仅授权本节点本身，不影响子节点和文档（极少用，特殊隔离场景）
    """
    ALL_DESCENDANTS = 'ALL_DESCENDANTS', _('本节点及所有后代')
    NODE_ONLY = 'NODE_ONLY', _('仅本节点')


class ShareStatus(models.TextChoices):
    """共享/黑名单状态机：ACTIVE → EXPIRED(到期)/REVOKED(撤销)"""
    ACTIVE = 'ACTIVE', _('生效中')
    EXPIRED = 'EXPIRED', _('已过期')
    REVOKED = 'REVOKED', _('已撤销')


class VisibilityLevel(models.TextChoices):
    """文档可见性层级 —— 无个人级，三档之一必选

    TEAM_ONLY：仅 team_id 对应团队可见（团队归属文档默认）
    DEPT_ONLY：仅 dept_id 对应部门（含下属团队）可见
    PUBLIC：全局全员可见（部门经理上推 → kb_admin 批准）
    """
    TEAM_ONLY = 'TEAM_ONLY', _('仅团队')
    DEPT_ONLY = 'DEPT_ONLY', _('仅部门')
    PUBLIC = 'PUBLIC', _('全局公开')


# ============================================================================
# KnowledgeNode（知识库节点树）
# ============================================================================

class KnowledgeNode(models.Model):
    """知识节点树 —— 路径枚举（Materialized Path）+ parent 双写模式

    path 选型：`/id1/id2/id3/` 格式，首尾加 `/` 分隔符避免前缀误匹配
    （节点 12 和 123 不会用 LIKE '/12/%' 误匹配，因为边界有 `/`）。

    鉴权继承：授权给某节点（ALL_DESCENDANTS）= 自动授权其所有后代节点 + 后代节点下文档
    通过 `path LIKE '/1/5/12/%'` 一次前缀索引扫描搞定，无需递归 CTE。

    层级（node_sync.py 自动同步前 3 层）：
      1=KB root / 2=dept / 3=team / 4+=业务分类（手动管理）
    """
    NODE_TYPE_CHOICES = [
        ('root', 'root'),
        ('folder', 'folder'),
        ('leaf', 'leaf'),
    ]

    NODE_KIND_CHOICES = [
        ('ROOT', '根节点'),
        ('ORG', '组织节点'),
        ('FOLDER', '文件夹'),
    ]

    id = models.BigAutoField(primary_key=True)
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE,
                               db_column='parent_id', related_name='children')
    root_type = models.CharField(max_length=32, help_text=_('根节点类型，加速检索过滤'))
    node_type = models.CharField(max_length=16, choices=NODE_TYPE_CHOICES, default='folder')
    node_kind = models.CharField(max_length=16, choices=NODE_KIND_CHOICES, default='FOLDER',
                                 help_text=_('节点性质: ROOT=根节点 / ORG=组织节点(部门/团队,由组织同步创建) / '
                                             'FOLDER=文件夹(手动创建,文档只能挂在文件夹下)'))
    node_level = models.SmallIntegerField(default=4,
                                           help_text=_('节点层级: 1=kb 2=dept 3=team 4+=业务分类'))
    # 节点可见范围：NULL=继承父级（文档可见性收敛的单源约束）
    visibility_level = models.CharField(
        max_length=32, choices=VisibilityLevel.choices, null=True, blank=True,
        help_text=_('节点可见范围: TEAM_ONLY/DEPT_ONLY/PUBLIC，NULL=继承父级（root 兜底 PUBLIC）'))
    name = models.CharField(max_length=128)
    path = models.CharField(max_length=512, default='/',
                             help_text=_('路径枚举 /kb_id/dept_id/team_id/cat_id/... 首尾加 /'))
    depth = models.SmallIntegerField(default=0)
    description = models.TextField(blank=True, default='')
    order_no = models.IntegerField(default=0)
    owner_user = models.ForeignKey('users.User', null=True, blank=True,
                                   on_delete=models.SET_NULL, db_column='owner_user_id',
                                   related_name='owned_nodes',
                                   help_text=_('节点 Owner，默认创建人，可作为节点级共享审批人'))
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    ref_id = models.BigIntegerField(null=True, blank=True,
                                     help_text=_('关联源对象 ID：node_level=2 存 dept.id，node_level=3 存 team.id'))
    created_by = models.ForeignKey('users.User', null=True, blank=True,
                                    on_delete=models.SET_NULL, db_column='created_by',
                                    related_name='created_nodes')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # 图谱/Wiki 防抖合并派发标记：文档完成时原子 check-and-set，
    # 仅首个文档触发任务派发，任务执行时批量处理节点下所有待构建文档
    graph_pending = models.BooleanField(default=False, help_text=_('图谱待构建标记（防抖合并派发）'))
    wiki_pending = models.BooleanField(default=False, help_text=_('Wiki 待构建标记（防抖合并派发）'))

    class Meta:
        db_table = 'knowledge_node'
        indexes = [
            models.Index(fields=['parent'], name='idx_kn_parent'),
            models.Index(fields=['root_type'], name='idx_kn_root_type'),
            models.Index(fields=['path'], name='idx_kn_path'),
        ]

    def __str__(self):
        return f'{self.root_type}:{self.name}'


# ============================================================================
# Document（文档元数据）
# ============================================================================

class Document(models.Model):
    """文档元数据 —— 归属团队或部门（二选一）+ visibility_level 三档可见

    归属设计（最终计划）：
    - 归属团队：team_id 必填，dept_id 可填（冗余 = team.department_id，加速部门级检索）
    - 归属部门：dept_id 必填，team_id 为空（部门公共文档，无具体 Owner 团队）
    - CHECK 约束：team_id 或 dept_id 至少一个非空

    可见性 visibility_level：
    - TEAM_ONLY：仅 team_id 团队可见（默认）
    - DEPT_ONLY：仅 dept_id 部门（含下属团队）可见
    - PUBLIC：全局全员可见

    轻量申请入口（最终计划）：
    - allow_share_request：是否允许他人发起分享申请
    - preview_content：是否允许预览正文（申请前预览）
    - preview_chunks：是否允许预览切片
    - 若 allow_share_request=True，其他用户可预览（元信息 + 可选正文/切片）并发起申请，Owner 单审
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
    # 图谱/Wiki 构建阶段状态（解析完成后由节点级防抖任务驱动）
    PIPELINE_STATUS_CHOICES = [
        ('pending', 'pending'),      # 待构建（等待节点级防抖任务）
        ('extracting', 'extracting'),  # 构建中
        ('done', 'done'),            # 构建完成
        ('failed', 'failed'),        # 构建失败（可手动重试）
        ('skipped', 'skipped'),      # 未启用（配置关闭或无数据可构建）
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
    AUDIT_STATUS_CHOICES = [
        ('pending_team', '待团队组长审核'),
        ('pending_compliance', '待合规复核'),
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

    # ── 归属（团队或部门二选一）─────────────────────────────
    node = models.ForeignKey(KnowledgeNode, on_delete=models.PROTECT, db_column='node_id',
                             related_name='documents',
                             help_text=_('挂载节点（团队/部门/业务分类节点均可）'))
    dept_id = models.BigIntegerField(null=True, blank=True,
                                     help_text=_('归属部门 ID（部门归属或团队归属冗余）'))
    team_id = models.BigIntegerField(null=True, blank=True,
                                     help_text=_('归属团队 ID（团队归属必填，部门归属为空）'))

    # ── 可见性与 Owner ─────────────────────────────────────
    visibility_level = models.CharField(max_length=32, choices=VisibilityLevel.choices,
                                        default=VisibilityLevel.TEAM_ONLY,
                                        help_text=_('可见性层级：TEAM_ONLY/DEPT_ONLY/PUBLIC'))
    owner = models.ForeignKey('users.User', on_delete=models.PROTECT, db_column='owner_id',
                              related_name='owned_documents', help_text=_('文档 Owner'))

    # ── 文件元信息 ─────────────────────────────────────────
    title = models.CharField(max_length=256)
    file_name = models.CharField(max_length=256)
    file_type = models.CharField(max_length=16, choices=FILE_TYPE_CHOICES)
    file_size = models.BigIntegerField(default=0)
    file_hash = models.CharField(max_length=64, help_text=_('sha256 文件内容哈希，用于版本识别'))
    file_path = models.CharField(max_length=512, blank=True, default='',
                                 help_text=_('本地存储路径或 OSS URL'))
    mime_type = models.CharField(max_length=64, blank=True, default='')

    # ── 权限冗余标志位（加速检索跳过空表查询）─────────────────
    has_block_user = models.BooleanField(default=False,
                                         help_text=_('是否有黑名单用户（加速检索跳过黑名单查询）'))
    has_resource_share = models.BooleanField(default=False,
                                             help_text=_('是否有跨范围共享（加速检索跳过共享查询）'))

    # ── 文档级权限开关 ─────────────────────────────────────
    allow_download = models.BooleanField(default=False, help_text=_('是否允许下载该文档'))
    allow_share = models.BooleanField(default=False, help_text=_('是否允许分享该文档'))
    # 轻量申请入口（最终计划）：allow_share_request=True 时，他人可预览并发起分享申请
    allow_share_request = models.BooleanField(default=False,
                                              help_text=_('是否允许他人发起分享申请'))
    preview_content = models.BooleanField(default=False,
                                          help_text=_('申请预览时是否允许查看正文'))
    preview_chunks = models.BooleanField(default=False,
                                         help_text=_('申请预览时是否允许查看切片'))

    # ── 审核与密级 ─────────────────────────────────────────
    secret_level = models.SmallIntegerField(choices=SECRET_LEVEL_CHOICES, default=1,
                                             help_text=_('密级 1普通~4绝密，4禁止 PUBLIC'))
    audit_status = models.CharField(max_length=32, choices=AUDIT_STATUS_CHOICES, default='pending_team')

    # ── 处理状态 ───────────────────────────────────────────
    root_type = models.CharField(max_length=32, help_text=_('冗余：根节点类型，加速检索过滤'))
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='pending',
                               help_text=_('处理状态: pending/parsing/.../done/failed'))
    error_message = models.TextField(blank=True, default='')
    chunk_count = models.IntegerField(default=0)
    # 图谱/Wiki 构建阶段状态：解析完成(done)后由节点级防抖任务驱动流转，
    # 失败态可手动重试；skipped=配置未启用或无数据可构建
    graph_status = models.CharField(max_length=16, choices=PIPELINE_STATUS_CHOICES, default='pending',
                                    help_text=_('图谱构建状态: pending/extracting/done/failed/skipped'))
    wiki_status = models.CharField(max_length=16, choices=PIPELINE_STATUS_CHOICES, default='pending',
                                   help_text=_('Wiki 构建状态: pending/extracting/done/failed/skipped'))

    # ── 版本管理 ───────────────────────────────────────────
    version = models.IntegerField(default=1)
    version_tag = models.CharField(max_length=64, default='', blank=True,
                                    help_text=_('版本标签，如 v1.0、v2.1'))
    # 活跃版本标记：同组（node+file_name+dept_id+team_id）通常仅一个活跃版本，
    # 检索与文档列表默认只召回活跃版本；?version=all 可回溯全部版本。
    # 注意：同组中"恰好同名但内容不同"的独立文档（如不同项目的同名代码文件）可并存多个活跃版本。
    is_active = models.BooleanField(default=True,
                                    help_text=_('是否活跃版本（检索/列表默认只召回活跃版本）'))
    # 文本类文件上传时截取的规范化内容样本，用于判定同组文件是「新版本」还是「独立文档」：
    # 相似度 >= 阈值视为新版本（旧版本自动置非活跃），否则视为独立文档（全部保留）。
    content_sample = models.TextField(blank=True, default='',
                                      help_text=_('文本内容样本（空白归一化后截断），用于版本相似度判定'))
    tags = ArrayField(models.CharField(max_length=32), default=list, blank=True)
    extra = models.JSONField(default=dict, blank=True)

    # ── 软删除 ─────────────────────────────────────────────
    is_deleted = models.BooleanField(default=False)
    delete_time = models.DateTimeField(null=True, blank=True, help_text=_('逻辑删除时间'))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    restored_at = models.DateTimeField(null=True, blank=True, help_text=_('恢复时间（用于审计追溯）'))
    restored_by = models.ForeignKey('users.User', null=True, blank=True,
                                    on_delete=models.SET_NULL, db_column='restored_by',
                                    related_name='restored_documents', help_text=_('恢复人'))

    class Meta:
        db_table = 'knowledge_document'
        indexes = [
            models.Index(fields=['dept_id'], name='idx_doc_dept'),
            models.Index(fields=['team_id'], name='idx_doc_team'),
            models.Index(fields=['node'], name='idx_doc_node'),
            models.Index(fields=['owner'], name='idx_doc_owner'),
            models.Index(fields=['audit_status'], name='idx_doc_audit_status'),
            models.Index(fields=['status'], name='idx_doc_status'),
            models.Index(fields=['visibility_level', 'root_type'], name='idx_doc_visroot'),
            models.Index(fields=['file_hash'], name='idx_doc_hash'),
            models.Index(fields=['node', 'file_name', 'dept_id', 'team_id', 'version_tag'],
                         name='idx_doc_node_name_version'),
        ]
        constraints = [
            # 同节点下同文件名+版本标签不重复（按部门/团队归属隔离：
            # 团队 A / 团队 B 各自上传同名文档互不冲突，也不互相触发版本替换）
            models.UniqueConstraint(
                fields=['node', 'file_name', 'dept_id', 'team_id', 'version_tag'],
                condition=models.Q(is_deleted=False),
                name='unique_doc_node_name_version',
            ),
            # 归属约束：非公开文档必须归属于团队或部门（无个人级文档）；
            # PUBLIC 全局公开文档允许无组织归属（如 root 下公共文件夹的全局文档）
            models.CheckConstraint(
                condition=models.Q(visibility_level=VisibilityLevel.PUBLIC)
                | models.Q(team_id__isnull=False)
                | models.Q(dept_id__isnull=False),
                name='doc_owner_scope_required',
            ),
        ]

    def __str__(self):
        return f'Doc<{self.id}>{self.title}'


# ============================================================================
# ResourceShare（统一资源主动共享表）
# ============================================================================

class ResourceShare(models.Model):
    """资源主动共享表 —— 单表 + share_scope_type 枚举（部门/团队/个人统一）

    设计要点（大厂标准，碾压三表分离方案）：
    - 单表 + 枚举：1 次 SQL 用 WHERE (... OR ... OR ...) 组合，加 LIMIT 1 命中即停
    - 覆盖索引：(resource_type, resource_id, share_scope_type, share_scope_id, status, ...)
      0 回表，扫描量收敛到"该资源下的几十条共享记录"
    - 扩展新共享类型（如角色组/外部用户）：只加枚举值，SQL 零改动

    节点级继承（resource_type=KNOWLEDGE_NODE + inherit_mode=ALL_DESCENDANTS）：
    - 含义：该节点 + 所有后代节点 + 后代节点下所有文档自动获得共享
    - 鉴权通过 KnowledgeNode.path 前缀匹配一次搞定（LIKE '/1/5/12/%'），无需递归

    唯一约束：UNIQUE(resource_type, resource_id, share_scope_type, share_scope_id)
    撤销后重新授予 = 产生新记录（历史保留，软删不物理删）。
    """
    id = models.BigAutoField(primary_key=True)
    resource_type = models.CharField(max_length=32, choices=ResourceType.choices,
                                     help_text=_('KNOWLEDGE_BASE/KNOWLEDGE_NODE/DOCUMENT'))
    resource_id = models.BigIntegerField(help_text=_('资源 ID：kb_id/node_id/doc_id（逻辑外键）'))
    share_scope_type = models.CharField(max_length=16, choices=ShareScopeType.choices,
                                        help_text=_('共享对象类型：DEPT/TEAM/USER'))
    share_scope_id = models.BigIntegerField(help_text=_('共享对象 ID：dept_id/team_id/user_id'))
    access_level = models.CharField(max_length=8, choices=AccessLevel.choices,
                                    default=AccessLevel.READ, help_text=_('READ/EDIT'))
    inherit_mode = models.CharField(max_length=16, choices=InheritMode.choices,
                                    default=InheritMode.ALL_DESCENDANTS,
                                    help_text=_('节点级专属：ALL_DESCENDANTS=子树继承/NODE_ONLY=仅本节点'))

    granted_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', help_text=_('授予人（Owner 或管理员）'))
    granted_at = models.DateTimeField(auto_now_add=True)
    effective_from = models.DateTimeField(null=True, blank=True, help_text=_('NULL = 立即生效'))
    expires_at = models.DateTimeField(null=True, blank=True, help_text=_('NULL = 永久有效'))
    status = models.CharField(max_length=16, choices=ShareStatus.choices,
                              default=ShareStatus.ACTIVE,
                              help_text=_('ACTIVE/EXPIRED/REVOKED'))
    revoked_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', help_text=_('撤销人'))
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'resource_share'
        verbose_name = _('资源共享')
        indexes = [
            # 方向 A（90% 场景）：判断「某资源 → 当前用户能不能看」—— 覆盖索引
            models.Index(fields=['resource_type', 'resource_id',
                                 'share_scope_type', 'share_scope_id', 'status'],
                         name='idx_share_resource_lookup'),
            # 方向 B（10% 场景）：反向查「某用户能看到哪些被共享的资源」
            models.Index(fields=['share_scope_type', 'share_scope_id',
                                 'status', 'resource_type', 'resource_id'],
                         name='idx_share_user_lookup'),
        ]
        constraints = [
            # 同一活跃共享对象不能重复授权（撤销后重新授予 = 新记录）
            models.UniqueConstraint(
                fields=['resource_type', 'resource_id',
                        'share_scope_type', 'share_scope_id'],
                name='unique_resource_share',
            ),
        ]

    def __str__(self):
        return f'Share<{self.resource_type}:{self.resource_id}->{self.share_scope_type}:{self.share_scope_id}>'


# ============================================================================
# ResourceBlockList（访问黑名单，仅个人，Deny Override 铁律）
# ============================================================================

class ResourceBlockList(models.Model):
    """访问黑名单表 —— 仅支持个人，Deny Override 优先级最高

    设计铁律：
    - Deny > Allow 不可变：共享给全部门 100 人，但有 1 人涉密/离职 —— 哪怕在 5 个白名单里也一律拒绝
    - 独立表、独立缓存、独立鉴权优先级，绝不和 Allow 混表
      （避免 SQL 逻辑判断顺序出错导致 Deny 被覆盖的致命事故）
    - 仅个人级：部门/团队不想给权限，直接从共享列表移除；拉黑是"个人级精准剔除"
      （避免「拉黑一个部门 = 整个部门 50 人都没权限」的灾难性误操作）

    节点级继承（block_inherit_mode=ALL_DESCENDANTS）：
    - 拉黑某节点 = 该节点 + 所有子节点 + 子节点下所有文档全部拒绝
    - 通过 KnowledgeNode.path 前缀匹配一次搞定

    鉴权判定顺序（见 access.py）：
    0. 黑名单（本表）命中 → 立即 403，不再执行任何后续白名单判定
    1. 系统级管理员（super_admin 等）
    2. 本组织自然可见范围
    3. 资源所有权（Owner）
    4. 跨范围共享白名单（ResourceShare）
    5. 兜底：不命中 = 不召回
    """
    id = models.BigAutoField(primary_key=True)
    resource_type = models.CharField(max_length=32, choices=ResourceType.choices,
                                     help_text=_('KNOWLEDGE_BASE/KNOWLEDGE_NODE/DOCUMENT'))
    resource_id = models.BigIntegerField(help_text=_('资源 ID：kb_id/node_id/doc_id'))
    blocked_user = models.ForeignKey('users.User', on_delete=models.CASCADE,
                                     related_name='blocked_resources',
                                     help_text=_('被封禁个人 user_id（唯一支持作用域 = 个人）'))
    block_inherit_mode = models.CharField(max_length=16, choices=InheritMode.choices,
                                          default=InheritMode.ALL_DESCENDANTS,
                                          help_text=_('节点级专属：ALL_DESCENDANTS=子树拉黑/NODE_ONLY=仅本节点'))
    reason = models.TextField(help_text=_('拉黑理由（必填，文本审计），如「已离职」「涉密项目剔除」'))
    blocked_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', help_text=_('操作人（Owner 或管理员）'))
    blocked_at = models.DateTimeField(auto_now_add=True)
    effective_from = models.DateTimeField(null=True, blank=True, help_text=_('NULL = 立即生效'))
    expires_at = models.DateTimeField(null=True, blank=True,
                                      help_text=_('NULL = 永久封禁；可设临时封禁（如 7 天后自动解封）'))
    status = models.CharField(max_length=16, choices=ShareStatus.choices,
                              default=ShareStatus.ACTIVE,
                              help_text=_('ACTIVE=封禁中/EXPIRED=到期自动解封/REVOKED=管理员手动解封'))
    revoked_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', help_text=_('解封人'))
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'resource_block_list'
        verbose_name = _('资源黑名单')
        indexes = [
            # 鉴权 99% 方向：给定"资源（或节点祖先链）+ 用户"→ 有没有命中黑名单？覆盖索引 0 回表
            models.Index(fields=['resource_type', 'resource_id', 'blocked_user', 'status'],
                         name='idx_block_check'),
            # 反查：某用户被哪些资源拉黑了
            models.Index(fields=['blocked_user', 'status', 'resource_type', 'resource_id'],
                         name='idx_block_user'),
        ]
        constraints = [
            # 同一人对同一资源不能重复封禁
            models.UniqueConstraint(
                fields=['resource_type', 'resource_id', 'blocked_user'],
                name='unique_resource_block',
            ),
        ]

    def __str__(self):
        return f'Block<{self.resource_type}:{self.resource_id}->user:{self.blocked_user_id}>'


# ============================================================================
# DocumentChunk / CodeChunk / ImageResource（保留，文档解析产物）
# ============================================================================

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
                condition=(
                    (models.Q(storage_mode='base64') & ~models.Q(base64_data=''))
                    | (models.Q(storage_mode='oss') & ~models.Q(oss_url=''))
                ),
                name='ck_image_storage_consistency',
            ),
        ]

    def __str__(self):
        return f'Image<{self.id}>{self.storage_mode}'


# ============================================================================
# DocOperationLog（文档业务操作日志，保留）
# ============================================================================

class DocOperationLog(models.Model):
    """知识库操作日志 — 只追加不删不改，完整审计链

    记录文档/节点的业务操作（上传/解析/删除/审核），用于业务审计追溯。
    权限操作（授权/共享/黑名单）的审计见 apps.users.models.PermissionAuditLog。
    两者分工：本表=文档业务，PermissionAuditLog=权限配置。
    """
    ACTION_CHOICES = [
        # 文档操作
        ('doc_create', '上传文档'),
        ('doc_delete', '删除文档'),
        ('doc_visibility_change', '修改可见范围'),
        ('doc_download', '下载文档'),
        ('doc_reparse', '重新解析'),
        ('doc_restore', '恢复文档'),
        ('doc_set_active', '切换活跃版本'),
        # 节点操作
        ('node_create', '创建节点'),
        ('node_update', '修改节点'),
        ('node_delete', '删除节点'),
        # 审核操作
        ('doc_audit_team_pass', '团队审核通过'),
        ('doc_audit_team_reject', '团队审核驳回'),
        ('doc_audit_compliance_pass', '合规复核通过'),
        ('doc_audit_compliance_reject', '合规复核驳回'),
        # 归档
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
