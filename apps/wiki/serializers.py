"""apps.wiki serializers —— Wiki 页面 / 章节 / 链接序列化器"""
from rest_framework import serializers

from apps.wiki.models import WikiPage, WikiSection, WikiLink


class WikiSectionSerializer(serializers.ModelSerializer):
    """Wiki 页面章节序列化器（详情页展示结构化章节）"""

    class Meta:
        model = WikiSection
        fields = ["id", "title", "content", "order"]


class WikiLinkSerializer(serializers.ModelSerializer):
    """Wiki 页面间自动链接序列化器"""

    target_page_id = serializers.IntegerField(read_only=True)
    target_title = serializers.CharField(source="target_page.title", read_only=True)

    class Meta:
        model = WikiLink
        fields = ["id", "target_page_id", "target_title", "link_text"]


class WikiPageListSerializer(serializers.ModelSerializer):
    """Wiki 页面列表序列化器（不含正文，列表页足够展示）"""

    node_name = serializers.CharField(source="node.name", read_only=True, default=None)
    node_path = serializers.CharField(source="node.path", read_only=True, default=None)
    root_type = serializers.CharField(source="node.root_type", read_only=True, default=None)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    expired_by_name = serializers.SerializerMethodField()
    # 当前用户视角：是否有权为该页面所在节点触发生成 / 刷新
    can_manage = serializers.SerializerMethodField()

    class Meta:
        model = WikiPage
        fields = [
            "id", "title", "summary", "status", "status_label", "tags",
            "node_id", "node_name", "node_path", "root_type",
            "community_id", "view_count",
            "expire_reason", "expired_at", "expired_by_name",
            "can_manage", "created_at", "updated_at",
        ]

    def get_expired_by_name(self, obj):
        """过期操作人用户名（系统自动过期 / 未被标记时为 None）"""
        return obj.expired_by.username if obj.expired_by_id else None

    def get_can_manage(self, obj) -> bool:
        """当前用户是否有权管理该 Wiki 页面（生成 / 刷新 / 标记过期）"""
        user = self.context.get("request").user if self.context.get("request") else None
        if not obj.node_id or not user:
            return False
        from apps.wiki.access import can_manage_wiki
        return can_manage_wiki(user, obj.node)


class WikiPageDetailSerializer(WikiPageListSerializer):
    """Wiki 页面详情序列化器（含正文 / 章节 / 链接 / 参考资料）"""

    sections = WikiSectionSerializer(many=True, read_only=True)
    outgoing_links = WikiLinkSerializer(many=True, read_only=True)
    incoming_links = WikiLinkSerializer(many=True, read_only=True)
    source_docs = serializers.SerializerMethodField()

    class Meta(WikiPageListSerializer.Meta):
        fields = WikiPageListSerializer.Meta.fields + [
            "content", "sections", "outgoing_links", "incoming_links", "source_docs",
        ]

    def get_source_docs(self, obj) -> list:
        """参考资料：节点下已完成文档（上限 20），附当前用户可访问标记

        列表为节点全量已完成文档（含当前用户不可读的），正文里的参考资料链接
        点击时前端会先在此列表按文件名/标题匹配，未命中再走 resolve_doc 兜底；
        管理员直接全量标记可访问。
        """
        if not obj.node_id:
            return []
        from apps.knowledge.access import filter_accessible_doc_ids
        from apps.knowledge.models import Document

        user = self.context.get("request").user if self.context.get("request") else None
        docs = list(
            Document.objects.filter(
                node_id=obj.node_id, is_deleted=False, status='done'
            ).order_by('id')[:20]
        )
        if not docs:
            return []
        if user and (getattr(user, 'is_super_admin', False) or getattr(user, 'is_kb_admin', False)):
            accessible_ids = {d.id for d in docs}
        else:
            accessible_ids = set(filter_accessible_doc_ids(user, [d.id for d in docs])) if user else set()
        return [{
            'id': d.id,
            'title': d.title,
            'file_name': d.file_name,
            'file_type': d.file_type,
            'can_access': d.id in accessible_ids,
        } for d in docs]
