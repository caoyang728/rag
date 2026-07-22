"""
agent views - 复杂任务拆分预览接口
- POST /api/v1/agent/task/plan/  只做拆分预览，不真跑
- POST /api/v1/agent/task/run/   实际拆分并逐个执行（stub 直接走 executor.ask）
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
    """POST /api/v1/agent/task/run/  {question, session_id?}"""
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
            from apps.agent.executor import ask as executor_ask
            result = executor_ask(
                user=request.user, question=q, session=session,
                use_cache=False, do_task_split=True,
            )
        except Exception as e:
            logger.exception("agent run error")
            return Response({"detail": f"内部错误: {e}"}, status=500)

        return Response({
            "session_id": session.id,
            "message_id": result.get("qa_id"),
            "answer": result.get("answer", ""),
            "citations": result.get("citations", []),
            "sub_tasks": result.get("sub_tasks", []),
            "stats": result.get("stats", {}),
        })
