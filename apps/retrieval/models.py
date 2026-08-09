"""
retrieval app - 向量检索 Model
对齐数据库设计 B5 document_vector
pgvector HNSW 索引 + 冗余权限字段（visibility_level/dept_id/team_id/owner_id/node_path）

一次 SQL 完成"向量相似度 + 权限过滤 + 节点路径过滤"，避免 N+1 查询。

冗余字段设计（与 Document 主表对齐，写入时由 vector_store.upsert_vector 同步）：
- visibility_level：TEAM_ONLY/DEPT_ONLY/PUBLIC（组织维度自然可见范围）
- dept_id / team_id：归属组织 ID（用于 DEPT_ONLY/TEAM_ONLY 过滤）
- owner_id：文档 Owner（Owner 直接可见）
- node_id / node_path：挂载节点 ID + 路径（节点级共享继承用 path 前缀匹配）
- has_resource_share：是否有跨范围共享（标志位，跳过空共享子查询）
- has_block_user：是否有黑名单（标志位，跳过空黑名单子查询）
- is_active：文档是否活跃版本（检索只召回活跃版本，切换活跃时由 vector_store 同步）
"""
from django.db import models
from django.contrib.postgres.fields import ArrayField
from pgvector.django import VectorField, HnswIndex

from apps.knowledge.models import Document, DocumentChunk
from apps.knowledge.models import VisibilityLevel


class DocumentVector(models.Model):
    """B5 document_vector - 向量表（核心检索表）

    冗余权限字段说明：
    - 检索时无需 JOIN document 表，直接用 WHERE visibility_level=? OR owner_id=? OR team_id IN(?)
    - HNSW 索引：m=16 建图连接数，ef_construction=64 构建质量，opclasses=vector_cosine_ops
    - 查询时会话级 SET LOCAL hnsw.ef_search=40 调节召回精度

    黑名单过滤策略：
    - 文档级黑名单：检索时用 NOT IN 子查询排除（has_block_user 标志位跳过空子查询）
    - 节点级黑名单：留 access.py 二次过滤（涉及 path 前缀匹配，SQL 复杂，召回后过滤可接受）
    """

    id = models.BigAutoField(primary_key=True)

    # 关联主表
    document = models.ForeignKey(Document, on_delete=models.CASCADE,
                                  db_column='document_id', related_name='vectors')
    chunk = models.OneToOneField(DocumentChunk, on_delete=models.CASCADE,
                                  db_column='chunk_id', related_name='vector')

    # 向量核心字段：pgvector 1024 维（BGE-M3 输出维度）
    embedding = VectorField(dimensions=1024, help_text='BGE-M3 向量，1024 维')

    # ── 冗余权限字段（与 Document 主表对齐）─────────────────────
    visibility_level = models.CharField(max_length=32,
                                        choices=VisibilityLevel.choices,
                                        default=VisibilityLevel.TEAM_ONLY,
                                        help_text='冗余：TEAM_ONLY/DEPT_ONLY/PUBLIC')
    dept_id = models.BigIntegerField(null=True, blank=True,
                                     help_text='冗余：归属部门 ID')
    team_id = models.BigIntegerField(null=True, blank=True,
                                     help_text='冗余：归属团队 ID')
    owner_id = models.BigIntegerField(help_text='冗余：文档 Owner user_id')
    root_type = models.CharField(max_length=32, help_text='冗余：根节点类型')
    node_id = models.BigIntegerField(help_text='冗余：挂载节点 ID')
    node_path = models.CharField(max_length=512, default='/',
                                  help_text='冗余：节点路径，支持前缀过滤（节点级共享继承）')
    # 标志位：加速检索跳过空子查询（90% 文档无共享/无黑名单，跳过子查询）
    has_resource_share = models.BooleanField(default=False, help_text='冗余：是否有跨范围共享')
    has_block_user = models.BooleanField(default=False, help_text='冗余：是否有黑名单用户')
    is_active = models.BooleanField(default=True, help_text='冗余：文档是否活跃版本（检索只召回活跃版本，由 vector_store 同步）')
    chunk_type = models.CharField(max_length=16, default='text', help_text='冗余：切片类型，方便按类型过滤')

    # 辅助字段：BM25 关键词 & 内容摘要，避免检索命中后回表
    content_preview = models.TextField(default='', help_text='切片前 200 字，用于快速展示')
    keywords = ArrayField(models.CharField(max_length=32), default=list, blank=True,
                           help_text='jieba 分词后的关键词，加速 BM25')

    embedding_model = models.CharField(max_length=64, default='bge-m3')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'retrieval_doc_vector'
        indexes = [
            # HNSW 向量索引（cosine 距离）
            HnswIndex(
                name='idx_dv_embedding_hnsw',
                fields=['embedding'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops'],
            ),
            # 冗余字段的组合索引，加速权限过滤
            models.Index(fields=['visibility_level', 'root_type'], name='idx_dv_vis_root'),
            models.Index(fields=['owner_id'], name='idx_dv_owner'),
            models.Index(fields=['team_id'], name='idx_dv_team'),
            models.Index(fields=['dept_id'], name='idx_dv_dept'),
            models.Index(fields=['node_id'], name='idx_dv_node'),
            models.Index(fields=['node_path'], name='idx_dv_path'),
            models.Index(fields=['document'], name='idx_dv_doc'),
        ]

    def __str__(self):
        return f'Vec<{self.id}>doc={self.document_id}'
