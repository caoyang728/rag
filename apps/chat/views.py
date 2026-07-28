"""
chat views - 会话 / 问答 / 反馈
- ChatAskView: 核心接口，调用 apps.agent.executor.ask 完成 RAG 问答
"""
from loguru import logger

from django.db.models import Q, F, OuterRef, Subquery
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.chat.models import QaRecord, QaFeedback
from apps.chat.serializers import (
    SessionSerializer, QaRecordSerializer, QaFeedbackSerializer,
)
from apps.memory.models import Session
from apps.knowledge.models import KnowledgeNode



class SessionViewSet(viewsets.ModelViewSet):
    """/api/v1/chat/sessions/"""
    serializer_class = SessionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        first_question_subq = QaRecord.objects.filter(session=OuterRef('pk')) \
            .order_by('turn_index') \
            .values('question')[:1]
        qs = Session.objects.filter(user=self.request.user, is_deleted=False) \
            .annotate(_first_question=Subquery(first_question_subq))

        search = self.request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(title__icontains=search) |
                Q(_first_question__icontains=search)
            )

        return qs.order_by("-last_active_at")

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def destroy(self, request, *args, **kwargs):
        s = self.get_object()
        s.is_deleted = True
        s.save(update_fields=["is_deleted"])
        return Response(status=204)

    @action(detail=True, methods=["get"])
    def qa(self, request, pk=None):
        """会话下所有问答记录"""
        s = self.get_object()
        records = QaRecord.objects.filter(session=s).order_by("turn_index")
        return Response(QaRecordSerializer(records, many=True).data)


class ChatAskView(APIView):
    """
    POST /api/v1/chat/ask/
    Body: {session_id?, question, mode?, root_types?[], node_ids?[], use_cache?, do_task_split?}
    Response: {answer, citations, message_id, session_id, stats}

    热点缓存 -> 任务拆分 -> 混合检索 -> 记忆加载 -> LLM 生成 -> 落 QaRecord 全链路
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        question = (request.data.get("question") or "").strip()
        if not question:
            return Response({"detail": "question 必填"}, status=400)

        session_id = request.data.get("session_id")
        mode = request.data.get("mode", "rag")  # rag / reasoning / mixed
        root_types = request.data.get("root_types")
        node_ids = request.data.get("node_ids")
        use_cache = bool(request.data.get("use_cache", True))
        do_task_split = bool(request.data.get("do_task_split", False))

        # 动态获取默认根类型
        if not root_types or not root_types[0]:
            default_root = KnowledgeNode.objects.filter(
                node_type='root', is_deleted=False
            ).first()
            root_types = [default_root.root_type] if default_root else ['company_doc']

        # 会话
        if session_id:
            try:
                session = Session.objects.get(id=session_id, user=request.user, is_deleted=False)
            except Session.DoesNotExist:
                return Response({"detail": "session 不存在"}, status=404)
        else:
            session = Session.objects.create(
                user=request.user,
                title=question[:32],
                root_type=root_types[0],
            )

        # 调用 executor
        try:
            from apps.agent.executor import ask as executor_ask
            result = executor_ask(
                user=request.user,
                question=question,
                session=session,
                root_types=root_types,
                node_ids=node_ids,
                use_cache=use_cache,
                do_task_split=do_task_split,
                do_rerank=True,
            )
        except Exception as e:
            logger.exception("chat.ask executor error")
            return Response({"detail": "服务端处理异常，请稍后重试"}, status=500)

        # 会话轮次+1（原子更新）
        Session.objects.filter(id=session.id).update(turn_count=F('turn_count') + 1)

        response_data = {
            "message_id": result.get("qa_id"),
            "session_id": session.id,
            "answer": result.get("answer", ""),
            "citations": result.get("citations", []),
            "is_hit_cache": result.get("is_hit_cache", False),
            "stats": result.get("stats", {}),
        }
        
        # 如果有错误信息，添加到响应中
        stats = result.get("stats", {})
        if stats.get("error"):
            response_data["error"] = stats.get("error")
        
        return Response(response_data)


class FeedbackView(APIView):
    """POST /api/v1/chat/feedback/  {qa_record_id, rating, tags?, comment?}"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        qa_id = request.data.get("qa_record_id") or request.data.get("message_id")
        rating = int(request.data.get("rating") or 0)
        if not qa_id:
            return Response({"detail": "qa_record_id 必填"}, status=400)

        try:
            qa = QaRecord.objects.get(id=qa_id, user=request.user)
        except QaRecord.DoesNotExist:
            return Response({"detail": "qa_record 不存在"}, status=404)

        obj, created = QaFeedback.objects.update_or_create(
            qa_record=qa,
            defaults={
                "user": request.user,
                "rating": rating,
                "tags": request.data.get("tags") or [],
                "comment": (request.data.get("comment") or "")[:2000],
            },
        )
        return Response(QaFeedbackSerializer(obj).data, status=201 if created else 200)


class QaRecordListView(APIView):
    """GET /api/v1/chat/records/?session_id=  用户的问答历史"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = QaRecord.objects.filter(user=request.user).order_by("-created_at")
        session_id = request.query_params.get("session_id")
        if session_id:
            qs = qs.filter(session_id=session_id)
        qs = qs[:200]
        return Response({"records": QaRecordSerializer(qs, many=True).data})
