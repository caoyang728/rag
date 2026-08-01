"""
system views
- GET  /api/v1/system/health/   健康检查（含 DB / Redis / LLM ping）
- GET  /api/v1/system/configs/  系统配置列表
- PUT  /api/v1/system/configs/<key>/  更新配置
- GET  /api/v1/system/stats/    简易看板：文档数/QA数/用户数
"""
from loguru import logger
import time

from django.db import connection
from django.contrib.auth import get_user_model
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.system.models import SystemConfig

User = get_user_model()


class HealthView(APIView):
    """GET /api/v1/system/health/  组件健康检查"""
    permission_classes = [AllowAny]

    def get(self, request):
        result = {"service": "rag-agent-backend", "ok": True, "components": {}}

        # DB
        t0 = time.time()
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
            result["components"]["db"] = {"ok": True, "ms": int((time.time() - t0) * 1000)}
        except Exception as e:
            result["ok"] = False
            result["components"]["db"] = {"ok": False, "error": str(e)}

        # Redis
        try:
            import redis
            from django.conf import settings
            r = redis.Redis.from_url(getattr(settings, "REDIS_URL",
                                             "redis://localhost:6379/0"))
            t0 = time.time()
            r.ping()
            result["components"]["redis"] = {"ok": True, "ms": int((time.time() - t0) * 1000)}
        except Exception as e:
            result["components"]["redis"] = {"ok": False, "error": str(e)[:120]}

        # LLM
        try:
            from apps.llm.factory import get_llm
            llm = get_llm()
            result["components"]["llm"] = {"ok": True, "provider": getattr(llm, "provider", "unknown")}
        except Exception as e:
            result["components"]["llm"] = {"ok": False, "error": str(e)[:120]}

        return Response(result)


class SystemConfigView(APIView):
    """GET/PUT /api/v1/system/configs/  or /api/v1/system/configs/<key>/"""
    permission_classes = [IsAuthenticated]

    def get(self, request, key=None):
        if key:
            try:
                c = SystemConfig.objects.get(key=key)
            except SystemConfig.DoesNotExist:
                return Response({"detail": "not found"}, status=404)
            return Response(self._ser(c))
        rows = SystemConfig.objects.all().order_by("key")
        return Response({"configs": [self._ser(c) for c in rows]})

    def put(self, request, key=None):
        # 系统配置写入仅超级管理员可操作，避免普通用户篡改运行参数
        if not getattr(request.user, 'is_super_admin', False):
            return Response({"detail": "仅超级管理员可修改系统配置"}, status=403)
        if not key:
            return Response({"detail": "key required"}, status=400)
        value = request.data.get("value", "")
        value_type = request.data.get("value_type", "string")
        obj, _ = SystemConfig.objects.update_or_create(
            key=key,
            defaults={"value": str(value), "value_type": value_type,
                      "updated_by": request.user if request.user.is_authenticated else None},
        )
        return Response(self._ser(obj))

    def _ser(self, c):
        return {"key": c.key, "value": c.value if not c.is_secret else "***",
                "value_type": c.value_type, "description": c.description,
                "is_secret": c.is_secret, "updated_at": c.updated_at.isoformat()}


class StatsView(APIView):
    """GET /api/v1/system/stats/  首页看板"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.knowledge.models import Document, KnowledgeNode
        from apps.chat.models import QaRecord
        stats = {
            "users": User.objects.filter(is_deleted=False).count(),
            "nodes": KnowledgeNode.objects.filter(is_deleted=False).count(),
            "documents": Document.objects.filter(is_deleted=False).count(),
            "documents_ready": Document.objects.filter(is_deleted=False, status="done").count(),
            "qa_records": QaRecord.objects.count(),
            "my_qa_records": QaRecord.objects.filter(user=request.user).count(),
        }
        return Response(stats)


class GlobalSearchView(APIView):
    """GET /api/v1/system/search/?q=keyword
    跨域搜索：文档、聊天会话、知识节点（按用户权限过滤）
    返回分组结果，最多各 10 条。
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.knowledge.models import Document, KnowledgeNode
        from apps.chat.models import Session
        from apps.retrieval.permission import build_permission_q

        q = (request.query_params.get("q") or "").strip()
        if not q:
            return Response({"query": "", "groups": {}})
        if len(q) > 64:
            return Response({"detail": "搜索关键词最多 64 个字符"}, status=400)

        # 文档（受权限过滤）
        doc_qs = Document.objects.filter(is_deleted=False, title__icontains=q)
        try:
            perm_q = build_permission_q(request.user)
            if perm_q:
                doc_qs = doc_qs.filter(perm_q)
        except Exception as e:
            logger.warning("build_permission_q failed: %s", e)
        doc_qs = doc_qs.order_by("-created_at")[:10]
        docs = [
            {
                "id": d.id,
                "type": "document",
                "title": d.title,
                "subtitle": d.file_name or "",
                "url": "/upload/",
                "icon": "📄",
                "created_at": d.created_at.isoformat() if d.created_at else "",
            }
            for d in doc_qs
        ]

        # 聊天会话（仅本人）
        sess_qs = Session.objects.filter(
            is_deleted=False, user=request.user, title__icontains=q
        ).order_by("-last_active_at")[:10]
        sessions = [
            {
                "id": s.id,
                "type": "session",
                "title": s.title,
                "subtitle": "会话记录",
                "url": "/chat/",
                "icon": "💬",
                "created_at": (s.last_active_at or s.created_at).isoformat() if (s.last_active_at or s.created_at) else "",
            }
            for s in sess_qs
        ]

        # 知识节点
        node_qs = KnowledgeNode.objects.filter(is_deleted=False, name__icontains=q)[:10]
        nodes = [
            {
                "id": n.id,
                "type": "node",
                "title": n.name,
                "subtitle": "知识库节点",
                "url": "/upload/",
                "icon": "🗂️",
                "created_at": "",
            }
            for n in node_qs
        ]

        return Response({
            "query": q,
            "groups": {
                "documents": docs,
                "sessions": sessions,
                "nodes": nodes,
            },
            "total": len(docs) + len(sessions) + len(nodes),
        })
