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
    # 当前用户视角：是否有权为该页面所在节点触发生成 / 刷新
    can_manage = serializers.SerializerMethodField()

    class Meta:
        model = WikiPage
        fields = [
            "id", "title", "summary", "status", "status_label", "tags",
            "node_id", "node_name", "node_path", "root_type",
            "community_id", "view_count",
            "can_manage", "created_at", "updated_at",
        ]

    def get_can_manage(self, obj) -> bool:
        """当前用户是否有权管理该 Wiki 页面（生成 / 刷新 / 标记过期）"""
        user = self.context.get("request").user if self.context.get("request") else None
        if not obj.node_id or not user:
            return False
        from apps.wiki.access import can_manage_wiki
        return can_manage_wiki(user, obj.node)


class WikiPageDetailSerializer(WikiPageListSerializer):
    """Wiki 页面详情序列化器（含正文 / 章节 / 链接）"""

    sections = WikiSectionSerializer(many=True, read_only=True)
    outgoing_links = WikiLinkSerializer(many=True, read_only=True)
    incoming_links = WikiLinkSerializer(many=True, read_only=True)

    class Meta(WikiPageListSerializer.Meta):
        fields = WikiPageListSerializer.Meta.fields + [
            "content", "sections", "outgoing_links", "incoming_links",
        ]
