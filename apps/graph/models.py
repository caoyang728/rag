"""
知识图谱数据模型
- GraphEntity: 实体表（人物/组织/概念/术语/产品）
- GraphRelation: 实体间关系表
- GraphCommunity: 社区表（Louvain 社区检测结果 + 摘要）
"""
from django.db import models
from django.contrib.postgres.fields import ArrayField
from pgvector.django import VectorField, HnswIndex


class GraphEntity(models.Model):
    """图谱实体表 —— 从文档切片中用 LLM 抽取并去重合并

    source_doc_ids 记录实体来源文档，用于文档删除/更新时的增量清理。
    embedding 为实体语义向量（description 生成），支持向量检索实体匹配。
    """

    TYPE_CHOICES = [
        ('PERSON', '人物'),
        ('ORG', '组织'),
        ('CONCEPT', '概念'),
        ('TERM', '术语'),
        ('PRODUCT', '产品'),
    ]

    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=256, db_index=True)
    type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    description = models.TextField(blank=True, default='')
    aliases = ArrayField(models.CharField(max_length=128), default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    source_doc_ids = ArrayField(models.BigIntegerField(), default=list, blank=True)
    embedding = VectorField(dimensions=1024, null=True, blank=True,
                            help_text='实体语义向量（BGE-M3 1024 维）')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'graph_entity'
        indexes = [
            HnswIndex(
                name='idx_ge_embedding_hnsw',
                fields=['embedding'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops'],
            ),
            models.Index(fields=['type'], name='idx_ge_type'),
        ]

    def __str__(self):
        return f'{self.name}({self.type})'


class GraphRelation(models.Model):
    """图谱关系表 —— 有向边，关联两个实体

    同一文档可能多次抽取同一对关系，通过 unique_together 去重合并 weight。
    """

    id = models.BigAutoField(primary_key=True)
    source_entity = models.ForeignKey(GraphEntity, on_delete=models.CASCADE,
                                      db_column='source_entity_id',
                                      related_name='outgoing_relations')
    target_entity = models.ForeignKey(GraphEntity, on_delete=models.CASCADE,
                                      db_column='target_entity_id',
                                      related_name='incoming_relations')
    relation_type = models.CharField(max_length=64, db_index=True)
    weight = models.FloatField(default=1.0)
    metadata = models.JSONField(default=dict, blank=True)
    source_doc_ids = ArrayField(models.BigIntegerField(), default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'graph_relation'
        indexes = [
            models.Index(fields=['source_entity', 'relation_type'],
                         name='idx_gr_source_rel'),
            models.Index(fields=['target_entity', 'relation_type'],
                         name='idx_gr_target_rel'),
        ]
        unique_together = [('source_entity', 'target_entity', 'relation_type')]

    def __str__(self):
        return f'{self.source_entity.name} --[{self.relation_type}]--> {self.target_entity.name}'


class GraphCommunity(models.Model):
    """图谱社区表 —— Louvain 社区检测结果

    同一组实体在不同 level 粒度下会生成多个社区记录（community_id + level 联合区分）。
    """

    id = models.BigAutoField(primary_key=True)
    community_id = models.IntegerField(db_index=True)
    level = models.SmallIntegerField(default=0,
                                     help_text='社区粒度：0=细 1=中 2=粗')
    entity_ids = ArrayField(models.BigIntegerField(), default=list)
    summary = models.TextField(blank=True, default='')
    keywords = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'graph_community'
        indexes = [
            models.Index(fields=['community_id', 'level'], name='idx_gc_id_level'),
        ]

    def __str__(self):
        return f'Community<{self.community_id}@{self.level}>'
