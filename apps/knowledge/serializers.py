"""knowledge serializers"""
from rest_framework import serializers
from apps.knowledge.models import KnowledgeNode, Document, DocumentChunk, VisibilityLevel
from apps.knowledge.access import resolve_doc_access


class KnowledgeNodeSerializer(serializers.ModelSerializer):
    children_count = serializers.SerializerMethodField()
    document_count = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = KnowledgeNode
        fields = [
            "id", "parent_id", "root_type", "node_type", "node_kind", "node_level",
            "visibility_level", "name", "path", "depth", "description", "order_no",
            "children_count", "document_count",
            "created_by", "created_by_name", "created_at", "updated_at",
        ]

    def get_children_count(self, obj):
        if hasattr(obj, "_children_count"):
            return obj._children_count
        return obj.children.filter(is_deleted=False).count()

    def get_document_count(self, obj):
        # 仅统计已通过双审（audit_status=passed）、未删除且活跃版本的文档：
        # 未通过审核/复核或已失效的旧版本不计入（与节点树/文档列表默认口径一致）
        if hasattr(obj, "_document_count"):
            return obj._document_count
        return obj.documents.filter(is_deleted=False, audit_status='passed', is_active=True).count()

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.real_name or obj.created_by.username
        return None


class KnowledgeNodeCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeNode
        fields = ["parent", "root_type", "node_type", "node_kind", "visibility_level",
                  "name", "description", "order_no"]
        extra_kwargs = {"root_type": {"required": False}, "node_kind": {"required": False},
                        "visibility_level": {"required": False}}

    def validate_parent(self, value):
        """校验父节点：不能是已删除、不能是叶子节点"""
        if value.is_deleted:
            raise serializers.ValidationError("上级节点不存在")
        if value.node_type == "leaf":
            raise serializers.ValidationError("不能在叶子节点下创建子节点")
        return value

    def validate_visibility_level(self, value):
        """可见范围必须为三档之一；空值（继承父级）由前端省略字段表达"""
        if value is not None and value not in VisibilityLevel.values:
            raise serializers.ValidationError("可见范围必须是 TEAM_ONLY/DEPT_ONLY/PUBLIC 之一")
        return value


def _doc_org_names(obj):
    """文档归属部门/团队名称（dept_id/team_id 为冗余字段而非外键）

    与 _doc_audit_row 规则一致：有 team_id 时团队优先、部门取团队所属部门；
    仅 dept_id 时只回填部门名。用于 detail/单个序列化场景的实时兜底查询，
    列表页由 view 层批量预取 _org_names 传入 context，避免 N+1。
    """
    from apps.users.models import Department, Team
    dept_name, team_name = '', ''
    if obj.team_id:
        t = Team.objects.filter(id=obj.team_id, is_deleted=False).only('name', 'department_id').first()
        if t:
            team_name = t.name
            d = Department.objects.filter(id=t.department_id, is_deleted=False).only('name').first()
            if d:
                dept_name = d.name
    elif obj.dept_id:
        d = Department.objects.filter(id=obj.dept_id, is_deleted=False).only('name').first()
        if d:
            dept_name = d.name
    return dept_name, team_name


class DocumentSerializer(serializers.ModelSerializer):
    node_name = serializers.CharField(source="node.name", read_only=True)
    owner_name = serializers.CharField(source="owner.username", read_only=True)
    restored_by_name = serializers.CharField(source="restored_by.username", read_only=True)
    # 归属部门/团队名称：列表页由 view 预取 _org_names（一次 SQL），detail 兜底实时查询
    dept_name = serializers.SerializerMethodField()
    team_name = serializers.SerializerMethodField()
    # 权限标志（当前用户视角）
    is_owner = serializers.SerializerMethodField()
    is_manager = serializers.SerializerMethodField()
    can_read = serializers.SerializerMethodField()
    can_download = serializers.SerializerMethodField()
    can_share = serializers.SerializerMethodField()
    # 前端友好字段：visibility_level → visible_scope（team/dept/public）
    visible_scope = serializers.SerializerMethodField()
    # 同组版本总数（node+file_name+dept_id+team_id），>1 时前端展示「版本切换」入口
    version_count = serializers.SerializerMethodField()
    # 驳回阶段（team=团队审核 / compliance=合规复核），驳回时由 DocAuditRejectView 写入 extra
    reject_stage = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            "id", "uuid", "node_id", "node_name", "title", "file_name", "file_type",
            "file_size", "file_hash", "mime_type", "owner_id", "owner_name",
            # 归属：node(FK) + dept_id + team_id（二选一非空）
            "dept_id", "team_id", "dept_name", "team_name",
            # 可见性层级：TEAM_ONLY / DEPT_ONLY / PUBLIC
            "visibility_level", "visible_scope", "secret_level", "audit_status",
            # 权限冗余标志位：has_block_user(黑名单) + has_resource_share(跨范围共享)
            "has_block_user", "has_resource_share", "allow_download", "allow_share",
            # 轻量申请入口（最终计划）
            "allow_share_request", "preview_content", "preview_chunks",
            "version", "version_tag", "tags", "is_active", "version_count",
            "root_type", "status", "error_message", "chunk_count",
            "graph_status", "wiki_status",
            "reject_stage",
            "is_deleted", "delete_time", "created_at", "updated_at",
            "restored_at", "restored_by", "restored_by_name",
            "is_owner", "is_manager", "can_read", "can_download", "can_share",
        ]
        read_only_fields = ["uuid", "file_hash", "status", "chunk_count",
                            "graph_status", "wiki_status",
                            "created_at", "updated_at", "restored_at", "restored_by",
                            # 活跃标志由版本管理逻辑维护，不允许通过 API 直接修改
                            "is_active", "version_count",
                            # 冗余标志位由授权操作维护，不允许直接通过 API 修改
                            "has_block_user", "has_resource_share"]

    def _access(self, obj):
        """调用 access.py 的 resolve_doc_access 获取当前用户对该文档的权限标志

        优先使用 view 层预计算的 _user_ctx（build_user_context）和 _grants_map（批量共享/黑名单），
        避免列表页 N+1 查询。
        """
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

    def get_visible_scope(self, obj):
        """visibility_level → 前端友好值（team/dept/public）"""
        return {
            'TEAM_ONLY': 'team',
            'DEPT_ONLY': 'dept',
            'PUBLIC': 'public',
        }.get(obj.visibility_level, obj.visibility_level)

    def get_dept_name(self, obj):
        if obj.dept_id:
            ctx = self.context.get("_org_names")
            if ctx is not None:
                return ctx.get("dept", {}).get(obj.dept_id, '')
            return _doc_org_names(obj)[0]
        return ''

    def get_team_name(self, obj):
        if obj.team_id:
            ctx = self.context.get("_org_names")
            if ctx is not None:
                return ctx.get("team", {}).get(obj.team_id, '')
            return _doc_org_names(obj)[1]
        return ''

    def get_version_count(self, obj):
        """同组版本总数：列表页优先用 view 层预计算的 _version_count_map（一次 SQL），
        详情/单个场景回退为实时统计（同组判定与上传/去重逻辑一致）"""
        ctx = self.context.get("_version_count_map")
        if ctx is not None:
            return ctx.get(obj.id, 1)
        from apps.knowledge.models import Document
        return Document.objects.filter(
            node=obj.node, file_name=obj.file_name,
            dept_id=obj.dept_id, team_id=obj.team_id, is_deleted=False,
        ).count()

    def get_reject_stage(self, obj):
        """驳回阶段（team=团队审核 / compliance=合规复核），空串表示未驳回"""
        return (obj.extra or {}).get('reject_stage', '')


class DocumentChunkSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentChunk
        fields = [
            "id", "document_id", "chunk_index", "chunk_type", "content",
            "content_length", "section_path", "page_number", "extra", "created_at",
        ]