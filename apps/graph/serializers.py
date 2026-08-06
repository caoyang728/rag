"""apps.graph serializers —— 图谱实体 / 社区序列化器

- EntityListSerializer：实体列表（不含大字段，列表页足够展示）
- EntityDetailSerializer：实体详情（含可见来源文档，source_docs 由视图按权限传入）
- CommunityListSerializer：社区列表（含主题/摘要/关键词）
- CommunityDetailSerializer：社区详情（含可见实体，entities 由视图按权限传入）
"""
from rest_framework import serializers

from apps.graph.models import GraphEntity, GraphCommunity


class EntityListSerializer(serializers.ModelSerializer):
    """实体列表序列化器（列表页展示：名称/类型/描述/来源数）"""

    type_label = serializers.CharField(source='get_type_display', read_only=True)
    source_doc_count = serializers.SerializerMethodField()

    class Meta:
        model = GraphEntity
        fields = [
            'id', 'name', 'type', 'type_label', 'description', 'aliases',
            'source_doc_count', 'created_at', 'updated_at',
        ]

    def get_source_doc_count(self, obj) -> int:
        """实体来源文档总数（含用户不可见的文档）"""
        return len(obj.source_doc_ids or [])


class EntityDetailSerializer(EntityListSerializer):
    """实体详情序列化器（含可见来源文档 + 向量状态）"""

    source_docs = serializers.SerializerMethodField()
    has_embedding = serializers.SerializerMethodField()

    class Meta(EntityListSerializer.Meta):
        fields = EntityListSerializer.Meta.fields + [
            'source_docs', 'has_embedding',
        ]

    def get_source_docs(self, obj) -> list:
        """用户可见的来源文档列表（[{id, title}]，由视图按权限预计算传入）"""
        return self.context.get('source_docs', [])

    def get_has_embedding(self, obj) -> bool:
        """是否已有语义向量（无向量实体无法参与语义检索）"""
        return obj.embedding is not None


class CommunityListSerializer(serializers.ModelSerializer):
    """社区列表序列化器（摘要/关键词/主题/实体数）"""

    topic = serializers.SerializerMethodField()
    entity_count = serializers.SerializerMethodField()

    class Meta:
        model = GraphCommunity
        fields = [
            'id', 'community_id', 'level', 'topic', 'summary',
            'keywords', 'entity_count', 'updated_at',
        ]

    def get_topic(self, obj) -> str:
        """社区主题（metadata.topic，LLM 摘要生成时写入）"""
        return (obj.metadata or {}).get('topic', '')

    def get_entity_count(self, obj) -> int:
        """社区内实体总数"""
        return len(obj.entity_ids or [])


class CommunityDetailSerializer(CommunityListSerializer):
    """社区详情序列化器（含社区内可见实体）"""

    entities = serializers.SerializerMethodField()

    class Meta(CommunityListSerializer.Meta):
        fields = CommunityListSerializer.Meta.fields + ['entities']

    def get_entities(self, obj) -> list:
        """社区内用户可见实体列表（[{id,name,type,type_label,description}]，视图按权限传入）"""
        return self.context.get('entities', [])
