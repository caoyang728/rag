"""
chat views - 会话 / 问答 / 反馈
- ChatAskStreamView: 核心 SSE 流式问答接口（同步 ChatAskView 已软删除）
"""
from loguru import logger

from django.db.models import Q, F, OuterRef, Subquery
from rest_framework import viewsets
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
        # prefetch_related 避免 AgentTrace 的 N+1 查询（序列化时读取工具调用链）
        records = QaRecord.objects.filter(session=s).prefetch_related('agent_traces').order_by("turn_index")
        return Response(QaRecordSerializer(records, many=True).data)



class ChatAskStreamView(APIView):
    """
    POST /api/v1/chat/ask_stream/  （SSE 流式问答）
    Body: {session_id?, question, mode?, root_types?[], node_ids?[], use_cache?, do_task_split?}

    响应内容类型：text/event-stream
    事件序列：start → (tool_call → tool_result)* → first_token → delta* → done  （或 error）
    - start:       {type, session_id, citations, is_hit_cache, is_agent?}
    - tool_call:   {type, call_id, tool_name, tool_args}        # Agent 模式工具调用开始
    - tool_result: {type, call_id, tool_name, ok, latency_ms, result_preview}  # 工具执行完成
    - first_token: {type, ttfb_ms}                              # 首字返回耗时（ms）
    - delta:       {type, delta}                                # 增量文本
    - done:        {type, message_id, session_id, citations, stats, tool_traces?}
    - error:       {type, detail}

    mode 取值：auto（默认）/ rag / agent，详见下方注释中的 ChatAskView 说明。
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        question = (request.data.get("question") or "").strip()
        if not question:
            return Response({"detail": "question 必填"}, status=400)

        session_id = request.data.get("session_id")
        mode = request.data.get("mode", "auto")  # auto / rag / agent
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

        # 会话解析/创建（同步完成，确保 session_id 在流开始前可用）
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

        # 调用流式 executor
        from apps.agent.executor import ask_stream as executor_ask_stream
        from apps.agent.streamer import stream_response

        # 会话轮次 +1：流式响应无法在"响应完成"后执行更新，故在此预先 +1。
        # 注意：不刷新 session 对象，executor 内仍以原 turn_count 计算 turn_index（=原值+1），
        # 与 DB 自增后的 turn_count 保持一致。错误路径也会落 QaRecord，故无需回滚。
        Session.objects.filter(id=session.id).update(turn_count=F('turn_count') + 1)

        gen = executor_ask_stream(
            user=request.user,
            question=question,
            session=session,
            root_types=root_types,
            node_ids=node_ids,
            use_cache=use_cache,
            do_task_split=do_task_split,
            do_rerank=True,
            mode=mode,
        )
        return stream_response(gen)


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
        # prefetch_related 避免 AgentTrace 的 N+1 查询（最多 200 条，每条都可能带工具调用链）
        qs = QaRecord.objects.filter(user=self.request.user).prefetch_related('agent_traces').order_by("-created_at")
        session_id = request.query_params.get("session_id")
        if session_id:
            qs = qs.filter(session_id=session_id)
        qs = qs[:200]
        return Response({"records": QaRecordSerializer(qs, many=True).data})
