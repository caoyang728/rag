"""
agent views - 复杂任务拆分预览接口
- POST /api/v1/agent/task/plan/  只做拆分预览，不真跑
- POST /api/v1/agent/task/run/   实际拆分并逐个执行（走 executor.ask_stream 流式）
"""
from loguru import logger
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView



class AgentTaskPlanView(APIView):
    """POST /api/v1/agent/task/plan/  {question}"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        q = (request.data.get("question") or "").strip()
        if not q:
            return Response({"detail": "question 必填"}, status=400)
        try:
            from apps.agent.task_splitter import maybe_split
            plan = maybe_split(q)
        except Exception as e:
            logger.exception("plan error")
            return Response({"detail": f"拆分失败: {e}"}, status=500)
        return Response(plan)


class AgentTaskRunView(APIView):
    """POST /api/v1/agent/task/run/  {question, session_id?}

    内部走 ask_stream 流式执行（输入/输出审查由 executor 内部处理），
    消费全部 SSE 事件后返回 JSON 结果。
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        q = (request.data.get("question") or "").strip()
        if not q:
            return Response({"detail": "question 必填"}, status=400)

        from apps.memory.models import Session
        sid = request.data.get("session_id")
        if sid:
            try:
                session = Session.objects.get(id=sid, user=request.user)
            except Session.DoesNotExist:
                return Response({"detail": "session 不存在"}, status=404)
        else:
            session = Session.objects.create(user=request.user, title=q[:32])

        try:
            from apps.agent.executor import ask_stream
            gen = ask_stream(
                user=request.user, question=q, session=session,
                use_cache=False, do_task_split=True,
            )
            # 消费流式事件，收集最终结果
            # 事件协议：start → first_token → (delta | content_filtered) → done
            answer_parts = []
            message_id = None
            citations = []
            stats = {}
            is_filtered = False
            filter_reason = None
            filter_category = None
            for event in gen:
                etype = event.get('type')
                if etype == 'delta':
                    answer_parts.append(event.get('delta', ''))
                elif etype == 'content_filtered':
                    # 命中 block：不下发 delta，发拦截事件
                    is_filtered = True
                    filter_reason = event.get('reason', '检测到违规内容，已拦截')
                    filter_category = event.get('category', 'other')
                elif etype == 'done':
                    message_id = event.get('message_id')
                    citations = event.get('citations', [])
                    stats = event.get('stats', {})
                    if event.get('is_filtered'):
                        is_filtered = True
                elif etype == 'error':
                    return Response(
                        {"detail": event.get('detail', '内部错误')}, status=500)

            answer = ''.join(answer_parts)

            if is_filtered:
                return Response({
                    "session_id": session.id,
                    "message_id": message_id,
                    "answer": answer,
                    "is_filtered": True,
                    "filter_reason": filter_reason or '检测到违规内容，已拦截',
                    "category": filter_category or 'other',
                    "citations": citations,
                    "stats": stats,
                })

            return Response({
                "session_id": session.id,
                "message_id": message_id,
                "answer": answer,
                "citations": citations,
                "stats": stats,
            })
        except Exception as e:
            logger.exception("agent run error")
            return Response({"detail": f"内部错误: {e}"}, status=500)
