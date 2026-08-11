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


class AgentWorkflowDetailView(APIView):
    """GET /api/v1/agent/workflows/{workflow_id}/

    工作流详情（含节点执行轨迹）：发起人本人或超管可查看。
    前端在人工确认通过/驳回后轮询此接口获取最终执行结果。
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, workflow_id):
        from apps.agent.models import AgentWorkflow

        try:
            workflow = AgentWorkflow.objects.get(id=workflow_id)
        except AgentWorkflow.DoesNotExist:
            return Response({"detail": "工作流不存在"}, status=404)
        # 权限：仅发起人本人或超管可见（轨迹含工具输入输出，属敏感信息）
        if workflow.user_id != request.user.id and not request.user.is_super_admin:
            return Response({"detail": "无权查看该工作流"}, status=403)

        nodes = []
        for r in workflow.node_runs.all():
            nodes.append({
                'node_id': r.node_id,
                'node_name': r.node_name,
                'step_type': r.step_type,
                'status': r.status,
                'attempt': r.attempt,
                'input': r.input,
                'output': (r.output or {}).get('output', '')[:2000],
                'ok': (r.output or {}).get('ok', False),
                'error': r.error[:500],
                'ticket_id': r.ticket_id,
                'latency_ms': r.latency_ms,
                'started_at': r.started_at.isoformat() if r.started_at else None,
                'finished_at': r.finished_at.isoformat() if r.finished_at else None,
            })

        result = workflow.result or {}
        return Response({
            'id': workflow.id,
            'question': workflow.question,
            'status': workflow.status,
            'status_display': workflow.get_status_display(),
            'max_nodes': workflow.max_nodes,
            'max_duration_sec': workflow.max_duration_sec,
            'definition': workflow.definition,
            'nodes': nodes,
            'result': {
                'answer': result.get('answer', ''),
                'citations': result.get('citations', []),
                'degraded_reasons': result.get('degraded_reasons', []),
                'qa_id': result.get('qa_id'),
                'filtered': result.get('filtered', False),
            },
            'created_at': workflow.created_at.isoformat() if workflow.created_at else None,
            'started_at': workflow.started_at.isoformat() if workflow.started_at else None,
            'finished_at': workflow.finished_at.isoformat() if workflow.finished_at else None,
        })


class WorkflowApprovalView(APIView):
    """POST /api/v1/agent/workflows/<workflow_id>/approve/

    工作流内嵌确认/拒绝（敏感工具节点的轻量级 HITL）。

    与工单审批的区别：
    - 工单审批（explicit approval 节点）：创建 TicketList，走正式审批流程，审批后由工单钩子恢复
    - 内嵌确认（敏感工具节点）：不创建工单，前端直接调用此接口确认/拒绝，同步恢复工作流

    Body: {"node_id": "xxx", "approved": true/false}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, workflow_id):
        from apps.agent.models import AgentWorkflow
        from apps.agent.workflow.engine import resume_workflow

        try:
            workflow = AgentWorkflow.objects.get(id=workflow_id)
        except AgentWorkflow.DoesNotExist:
            return Response({"detail": "工作流不存在"}, status=404)
        # 权限：仅发起人本人或超管可操作
        if workflow.user_id != request.user.id and not request.user.is_super_admin:
            return Response({"detail": "无权操作该工作流"}, status=403)
        if workflow.status != 'waiting_approval':
            return Response({"detail": f"工作流状态为 {workflow.status}，无需审批"}, status=400)

        node_id = request.data.get('node_id')
        approved = bool(request.data.get('approved', True))
        if not node_id:
            return Response({"detail": "node_id 必填"}, status=400)

        # 校验节点存在且处于 blocked 状态
        node_run = workflow.node_runs.filter(node_id=node_id).first()
        if not node_run:
            return Response({"detail": "节点不存在"}, status=404)
        if node_run.status != 'blocked':
            return Response({"detail": f"节点状态为 {node_run.status}，无需审批"}, status=400)

        # 同步恢复工作流
        try:
            resume_workflow(workflow, node_id=node_id, approved=approved)
        except Exception as e:
            logger.exception(f'[WorkflowApproval] resume failed: workflow={workflow.id}')
            return Response({"detail": f"恢复工作流失败: {e}"}, status=500)

        return Response({
            "workflow_id": workflow.id,
            "node_id": node_id,
            "approved": approved,
            "status": workflow.status,
        })


class AgentWorkflowListView(APIView):
    """GET /api/v1/agent/workflows/?status=running

    当前用户的工作流列表（按创建时间倒序），供前端展示工作流记录与审批中状态。
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.agent.models import AgentWorkflow

        qs = AgentWorkflow.objects.filter(user=request.user)
        status = request.query_params.get('status')
        if status:
            qs = qs.filter(status=status)
        workflows = qs[:50]
        return Response([{
            'id': w.id,
            'question': w.question,
            'status': w.status,
            'status_display': w.get_status_display(),
            'qa_id': (w.result or {}).get('qa_id'),
            'created_at': w.created_at.isoformat() if w.created_at else None,
            'finished_at': w.finished_at.isoformat() if w.finished_at else None,
        } for w in workflows])
