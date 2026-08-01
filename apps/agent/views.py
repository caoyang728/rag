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

        # 输入侧审查：与 ask_stream / agent_ask_stream 入口审查一致，
        # 命中 block 直接拒答，避免浪费任务拆分/检索/LLM 算力
        try:
            from apps.security.sensitive_filter import get_sensitive_filter
            _sf = get_sensitive_filter()
            _hits = _sf.check(q)
            _block = [h for h in _hits if h.action == 'block']
            if _block:
                return Response({
                    "detail": "检测到违规内容，已拦截",
                    "is_filtered": True,
                    "category": _block[0].category,
                }, status=403)
        except Exception:
            logger.exception('[AgentTaskRun] question input filter failed, skip input review')

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

        # 输出侧审查：ask 同步路径不做流式审查，这里对最终答案做一次性全量审查
        # 命中 block 时不返回违规内容，仅返回拦截提示
        answer = result.get("answer", "")
        try:
            from apps.agent.executor import _check_full_text
            safe_answer, out_hit = _check_full_text(answer)
            if out_hit:
                return Response({
                    "session_id": session.id,
                    "message_id": result.get("qa_id"),
                    "answer": "",
                    "is_filtered": True,
                    "filter_reason": "检测到违规内容，已拦截",
                    "category": getattr(out_hit, 'category', 'other'),
                    "citations": [],
                    "sub_tasks": result.get("sub_tasks", []),
                    "stats": result.get("stats", {}),
                })
            result["answer"] = safe_answer
        except Exception:
            logger.exception('[AgentTaskRun] output filter failed, skip output review')

        return Response({
            "session_id": session.id,
            "message_id": result.get("qa_id"),
            "answer": result.get("answer", ""),
            "citations": result.get("citations", []),
            "sub_tasks": result.get("sub_tasks", []),
            "stats": result.get("stats", {}),
        })
