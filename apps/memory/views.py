"""
memory views - 记忆调试接口 + 用户记忆偏好
"""
from loguru import logger
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView


class MemoryDebugView(APIView):
    """
    GET /api/v1/memory/context/?session_id=&question=
    返回当前会话的记忆上下文（short_term + session_memory + user_memory + global_memory）
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        session_id = request.query_params.get("session_id")
        question = request.query_params.get("question", "")
        if not session_id:
            return Response({"detail": "session_id 必填"}, status=400)

        from apps.memory.models import Session
        try:
            session = Session.objects.get(id=session_id, user=request.user)
        except Session.DoesNotExist:
            return Response({"detail": "session 不存在"}, status=404)

        try:
            from apps.memory.manager import MemoryManager
            mm = MemoryManager()
            ctx = mm.load_context(request.user, session, question)
        except Exception as e:
            logger.exception("memory ctx error")
            return Response({"detail": f"内部错误: {e}"}, status=500)
        return Response(ctx)


class RefineMemoryView(APIView):
    """POST /api/v1/memory/refine/  {session_id}  强制触发一次会话记忆提炼"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        session_id = request.data.get("session_id")
        if not session_id:
            return Response({"detail": "session_id 必填"}, status=400)
        # 校验会话归属，防止越权触发他人会话的记忆提炼
        from apps.memory.models import Session
        if not Session.objects.filter(id=session_id, user=request.user).exists():
            return Response({"detail": "session 不存在"}, status=404)
        try:
            from apps.memory.tasks import refine_session_memory
            refine_session_memory.delay(session_id)
        except Exception:
            logger.exception("refine dispatch error")
        return Response({"ok": True, "session_id": session_id})


class UserMemoryView(APIView):
    """GET/PATCH /api/v1/memory/user-memory/
    GET: 返回当前用户的记忆偏好（domain_tags, frequent_topics, preferences, profile_text）
    PATCH: 更新 domain_tags, frequent_topics, preferences（output_preference）
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.memory.models import UserMemory
        um, _ = UserMemory.objects.get_or_create(user=request.user)
        return Response({
            "domain_tags": um.domain_tags or [],
            "frequent_topics": um.frequent_topics or [],
            "preferences": um.preferences or {},
            "profile_text": um.profile_text or "",
        })

    def patch(self, request):
        from apps.memory.models import UserMemory
        um, _ = UserMemory.objects.get_or_create(user=request.user)
        if "domain_tags" in request.data:
            tags = request.data["domain_tags"]
            if isinstance(tags, list):
                um.domain_tags = [t.strip()[:32] for t in tags if t and isinstance(t, str) and t.strip()]
            else:
                return Response({"detail": "domain_tags 必须是数组"}, status=400)
        if "frequent_topics" in request.data:
            topics = request.data["frequent_topics"]
            if isinstance(topics, list):
                um.frequent_topics = [t.strip()[:64] for t in topics if t and isinstance(t, str) and t.strip()]
            else:
                return Response({"detail": "frequent_topics 必须是数组"}, status=400)
        if "preferences" in request.data:
            prefs = request.data["preferences"]
            if isinstance(prefs, dict):
                um.preferences = {k: str(v)[:512] for k, v in prefs.items() if k and v}
            else:
                return Response({"detail": "preferences 必须是对象"}, status=400)
        if "output_preference" in request.data:
            um.preferences = {**(um.preferences or {}), "output_preference": str(request.data["output_preference"])[:512]}
        um.save()
        return Response({"ok": True})
