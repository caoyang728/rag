"""
LLM Wiki 数据模型
- WikiPage: Wiki 知识页面（挂载到知识节点或图谱社区）
- WikiSection: 页面章节（预留，可记录结构化章节）
- WikiLink: 页面间自动链接
"""
from django.db import models
from django.contrib.postgres.fields import ArrayField
from pgvector.django import VectorField, HnswIndex


class WikiPage(models.Model):
    """Wiki 页面 —— LLM 基于知识节点文档或图谱社区自动生成

    node / community 二选一挂载：
    - node: 知识库节点（基于节点下文档生成）
    - community: 图谱社区（基于社区摘要生成）
    """

    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('published', '已发布'),
        ('expired', '已过期'),
    ]

    id = models.BigAutoField(primary_key=True)
    title = models.CharField(max_length=256)
    node = models.ForeignKey('knowledge.KnowledgeNode', on_delete=models.SET_NULL,
                             null=True, blank=True, db_column='node_id')
    community = models.ForeignKey('graph.GraphCommunity', on_delete=models.SET_NULL,
                                  null=True, blank=True, db_column='community_id')
    summary = models.TextField(blank=True, default='')
    content = models.TextField(blank=True, default='', help_text='Wiki 正文，Markdown 格式')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='draft')
    embedding = VectorField(dimensions=1024, null=True, blank=True,
                            help_text='页面语义向量（BGE-M3 1024 维）')
    tags = ArrayField(models.CharField(max_length=32), default=list, blank=True)
    view_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wiki_page'
        indexes = [
            HnswIndex(name='idx_wp_embedding_hnsw', fields=['embedding'],
                      m=16, ef_construction=64, opclasses=['vector_cosine_ops']),
            models.Index(fields=['status']),
            models.Index(fields=['node']),
            models.Index(fields=['community']),
        ]

    def __str__(self):
        return f'Wiki<{self.id}>{self.title}'


class WikiSection(models.Model):
    """Wiki 页面章节"""

    id = models.BigAutoField(primary_key=True)
    page = models.ForeignKey(WikiPage, on_delete=models.CASCADE, db_column='page_id',
                             related_name='sections')
    title = models.CharField(max_length=256)
    content = models.TextField(blank=True, default='')
    order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'wiki_section'
        ordering = ['order']
        indexes = [models.Index(fields=['page'])]

    def __str__(self):
        return f'Section<{self.id}>{self.title}'


class WikiLink(models.Model):
    """Wiki 页面间自动链接"""

    id = models.BigAutoField(primary_key=True)
    source_page = models.ForeignKey(WikiPage, on_delete=models.CASCADE,
                                    db_column='source_page_id', related_name='outgoing_links')
    target_page = models.ForeignKey(WikiPage, on_delete=models.CASCADE,
                                    db_column='target_page_id', related_name='incoming_links')
    link_text = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'wiki_link'
        unique_together = [('source_page', 'target_page')]

    def __str__(self):
        return f'{self.source_page.title} -> {self.target_page.title}'
