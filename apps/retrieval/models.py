"""
retrieval app - 向量检索 Model
对齐数据库设计 B5 document_vector
pgvector HNSW 索引 + 7 个冗余权限字段（visible_scope/owner_id/team_node_id/dept_node_id/root_type/node_path）
一次 SQL 完成"向量相似度 + 权限过滤 + 节点路径过滤"，避免 N+1 查询
"""
from django.db import models
from django.contrib.postgres.fields import ArrayField
from pgvector.django import VectorField, HnswIndex

from apps.knowledge.models import Document, DocumentChunk


class DocumentVector(models.Model):
    """B5 document_vector - 向量表（核心检索表）

    冗余权限字段说明：
    - 检索时无需 JOIN document 表，直接用 WHERE visible_scope=? OR owner_id=? OR team_node_id IN(?)
    - HNSW 索引：m=16 建图连接数，ef_construction=64 构建质量，opclasses=vector_cosine_ops
    - 查询时会话级 SET LOCAL hnsw.ef_search=40 调节召回精度
    """

    id = models.BigAutoField(primary_key=True)

    # 关联主表
    document = models.ForeignKey(Document, on_delete=models.CASCADE,
                                  db_column='document_id', related_name='vectors')
    chunk = models.OneToOneField(DocumentChunk, on_delete=models.CASCADE,
                                  db_column='chunk_id', related_name='vector')

    # 向量核心字段：pgvector 1024 维（BGE-M3 输出维度）
    embedding = VectorField(dimensions=1024, help_text='BGE-M3 向量，1024 维')

    # 冗余权限字段（8个）：一次 WHERE 搞定权限过滤
    visible_scope = models.CharField(max_length=16, default='team',
                                      help_text='冗余：team/dept/public')
    has_cross_team = models.BooleanField(default=False, help_text='冗余：是否有跨团队授权')
    has_allow_user = models.BooleanField(default=False, help_text='冗余：是否有个人白名单')
    owner_id = models.BigIntegerField(help_text='冗余：文档拥有者 user_id')
    team_node_id = models.BigIntegerField(null=True, blank=True,
                                           help_text='冗余：归属团队节点 id')
    dept_node_id = models.BigIntegerField(null=True, blank=True,
                                           help_text='冗余：归属部门节点 id')
    root_type = models.CharField(max_length=32, help_text='冗余：根节点类型')
    node_id = models.BigIntegerField(help_text='冗余：所属叶子节点 id')
    node_path = models.CharField(max_length=512, default='/',
                                  help_text='冗余：节点路径，支持前缀过滤')
    chunk_type = models.CharField(max_length=16, default='text',
                                   help_text='冗余：切片类型，方便按类型过滤')

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
            models.Index(fields=['visible_scope', 'root_type'], name='idx_dv_vis_root'),
            models.Index(fields=['owner_id'], name='idx_dv_owner'),
            models.Index(fields=['team_node_id'], name='idx_dv_team'),
            models.Index(fields=['dept_node_id'], name='idx_dv_dept'),
            models.Index(fields=['node_id'], name='idx_dv_node'),
            models.Index(fields=['node_path'], name='idx_dv_path'),
            models.Index(fields=['document'], name='idx_dv_doc'),
        ]

    def __str__(self):
        return f'Vec<{self.id}>doc={self.document_id}'
