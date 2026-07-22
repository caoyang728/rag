"""
notification views - 订阅管理 & 发送记录
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.notification.models import EmailSubscription, EmailSendLog


class SubscriptionView(APIView):
    """GET/PUT/PATCH /api/v1/notification/subscriptions/
    GET: 返回当前用户的所有订阅状态
    PUT: 更新单个 category 的订阅状态
    PATCH: 批量更新多个 category 的订阅状态 {subscriptions: {category: true/false, ...}}
    """
    permission_classes = [IsAuthenticated]

    # 支持的订阅类别及其默认值和前端映射
    DEFAULT_SUBS = {
        'node_update': {'label': '知识库节点更新', 'default': True},
        'system_notice': {'label': '系统告警通知', 'default': True},
        'daily_report': {'label': '每周报表推送', 'default': False},
        'keyword_alert': {'label': '关键词命中通知', 'default': True},
    }

    def _get_user_subs(self, user):
        """获取用户所有订阅，返回 dict {category: is_enabled}"""
        rows = EmailSubscription.objects.filter(user=user).values("category", "is_enabled")
        subs = {}
        for r in rows:
            subs[r["category"]] = r["is_enabled"]
        # 补齐默认值
        for cat, info in self.DEFAULT_SUBS.items():
            if cat not in subs:
                subs[cat] = info['default']
        return subs

    def get(self, request):
        subs = self._get_user_subs(request.user)
        result = {}
        for cat, info in self.DEFAULT_SUBS.items():
            result[cat] = {
                "is_enabled": subs.get(cat, info['default']),
                "label": info['label'],
            }
        return Response({"subscriptions": result})

    def put(self, request):
        category = request.data.get("category")
        enabled = bool(request.data.get("is_enabled", True))
        if not category:
            return Response({"detail": "category 必填"}, status=400)
        obj, _ = EmailSubscription.objects.update_or_create(
            user=request.user, category=category,
            defaults={"is_enabled": enabled},
        )
        return Response({"ok": True, "category": obj.category, "is_enabled": obj.is_enabled})

    def patch(self, request):
        """批量更新订阅"""
        subscriptions = request.data.get("subscriptions", {})
        if not isinstance(subscriptions, dict):
            return Response({"detail": "subscriptions 必须是对象"}, status=400)
        for cat, enabled in subscriptions.items():
            EmailSubscription.objects.update_or_create(
                user=request.user, category=cat,
                defaults={"is_enabled": bool(enabled)},
            )
        return Response({"ok": True, "updated": list(subscriptions.keys())})


class SendLogView(APIView):
    """GET /api/v1/notification/send-logs/  最近发送记录"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = list(EmailSendLog.objects.all().order_by("-id")[:100].values(
            "id", "to_email", "subject", "category", "status", "error_message", "created_at"
        ))
        return Response({"rows": rows, "count": len(rows)})
