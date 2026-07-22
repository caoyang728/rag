"""knowledge serializers"""
from rest_framework import serializers
from apps.knowledge.models import KnowledgeNode, Document, DocumentChunk


class KnowledgeNodeSerializer(serializers.ModelSerializer):
    children_count = serializers.SerializerMethodField()
    document_count = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = KnowledgeNode
        fields = [
            "id", "parent_id", "root_type", "node_type", "name", "path", "depth",
            "description", "order_no", "children_count", "document_count",
            "created_by", "created_by_name", "created_at", "updated_at",
        ]

    def get_children_count(self, obj):
        if hasattr(obj, "_children_count"):
            return obj._children_count
        return obj.children.filter(is_deleted=False).count()

    def get_document_count(self, obj):
        if hasattr(obj, "_document_count"):
            return obj._document_count
        return obj.documents.filter(is_deleted=False).count()

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.real_name or obj.created_by.username
        return None


class KnowledgeNodeCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeNode
        fields = ["parent", "root_type", "node_type", "name", "description", "order_no"]
        extra_kwargs = {"root_type": {"required": False}}

    def validate_parent(self, value):
        """校验父节点：不能是已删除、不能是叶子节点"""
        if value.is_deleted:
            raise serializers.ValidationError("上级节点不存在")
        if value.node_type == "leaf":
            raise serializers.ValidationError("不能在叶子节点下创建子节点")
        return value


class DocumentSerializer(serializers.ModelSerializer):
    node_name = serializers.CharField(source="node.name", read_only=True)
    owner_name = serializers.CharField(source="owner.username", read_only=True)

    class Meta:
        model = Document
        fields = [
            "id", "uuid", "node_id", "node_name", "title", "file_name", "file_type",
            "file_size", "file_hash", "mime_type", "owner_id", "owner_name",
            "visibility", "root_type", "status", "error_message", "chunk_count",
            "version", "tags", "created_at", "updated_at",
        ]
        read_only_fields = ["uuid", "file_hash", "status", "chunk_count", "created_at", "updated_at"]


class DocumentChunkSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentChunk
        fields = [
            "id", "document_id", "chunk_index", "chunk_type", "content",
            "content_length", "section_path", "page_number", "extra", "created_at",
        ]
