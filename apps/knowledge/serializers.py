"""knowledge serializers"""
from rest_framework import serializers
from apps.knowledge.models import KnowledgeNode, Document, DocumentChunk
from apps.knowledge.access import resolve_doc_access


class KnowledgeNodeSerializer(serializers.ModelSerializer):
    children_count = serializers.SerializerMethodField()
    document_count = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = KnowledgeNode
        fields = [
            "id", "parent_id", "root_type", "node_type", "node_level",
            "name", "path", "depth", "description", "order_no",
            "children_count", "document_count",
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
    restored_by_name = serializers.CharField(source="restored_by.username", read_only=True)
    # 权限标志（当前用户视角）
    is_owner = serializers.SerializerMethodField()
    is_manager = serializers.SerializerMethodField()
    can_read = serializers.SerializerMethodField()
    can_download = serializers.SerializerMethodField()
    can_share = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            "id", "uuid", "node_id", "node_name", "title", "file_name", "file_type",
            "file_size", "file_hash", "mime_type", "owner_id", "owner_name",
            "kb_node_id", "dept_node_id", "team_node_id", "category_node_id",
            "visible_scope", "secret_level", "audit_status",
            "has_deny_user", "has_cross_team", "has_allow_user", "allow_download", "allow_share",
            "root_type", "status", "error_message", "chunk_count", "version", "tags",
            "is_deleted", "delete_time", "created_at", "updated_at",
            "restored_at", "restored_by", "restored_by_name",
            "is_owner", "is_manager", "can_read", "can_download", "can_share",
        ]
        read_only_fields = ["uuid", "file_hash", "status", "chunk_count",
                            "created_at", "updated_at", "restored_at", "restored_by"]

    def _access(self, obj):
        request = self.context.get("request")
        ctx = self.context.get("_user_ctx")
        grants_map = self.context.get("_grants_map")
        return resolve_doc_access(request.user if request else None, obj, ctx=ctx, grants_map=grants_map)

    def get_is_owner(self, obj):
        return self._access(obj)["is_owner"]

    def get_is_manager(self, obj):
        return self._access(obj)["is_manager"]

    def get_can_read(self, obj):
        return self._access(obj)["can_read"]

    def get_can_download(self, obj):
        return self._access(obj)["can_download"]

    def get_can_share(self, obj):
        return self._access(obj)["can_share"]


class DocumentChunkSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentChunk
        fields = [
            "id", "document_id", "chunk_index", "chunk_type", "content",
            "content_length", "section_path", "page_number", "extra", "created_at",
        ]