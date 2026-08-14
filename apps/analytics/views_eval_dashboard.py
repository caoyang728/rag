"""
analytics views - 评估看板（DeepEval 12 维生产评估结果展示）
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.services.eval_dashboard_service import _DIMENSION_GROUPS
from apps.chat.models import QaRecord
from apps.users.permissions import CanViewAnalytics

# ============================================================================
# 评估看板 (DeepEval 12 维生产评估结果展示)
# ============================================================================
# 参考 LangSmith / Phoenix / Datadog 风格:
# - overview: 顶部 KPI(12 维均分按 4 大类分组 + 评估量 + 低分占比 + 安全告警)
# - trend: 时间序列(按天聚合,支持维度切换)
# - low-score-qa: 低分对话 Top N(按 QA 总分升序)
# - qa-detail: 单条 QA 完整明细(12 维 score+reason + 完整对话)
# 聚合逻辑统一下沉到 services/eval_dashboard_service.py，
# 本文件视图只做参数解析（宽松输入 serializer）与 Response 包装。


class EvalDashboardOverviewView(APIView):
    """GET /api/v1/analytics/eval-dashboard/overview/?days=7&root_type=&dept_id=&team_id=

    看板顶部 KPI:
    - total_evaluated: 评估过的 QA 数(distinct qa_record_id)
    - total_qa: 时间窗口内 QA 总数(用于覆盖率计算)
    - low_score_count: 平均分 < threshold 的 QA 数
    - safety_alert_count: toxicity<0.5 或 bias<0.5 的 QA 数(安全告警)
    - dimension_groups: 4 大类均分 + 各维度均分(雷达图用)
    组织筛选(dept_id/team_id):按提问用户的部门/团队归属过滤,team 有值时忽略 dept。
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request):
        from apps.analytics.serializers import EvalDashboardOverviewQuerySerializer
        from apps.analytics.services.eval_dashboard_service import get_eval_dashboard_overview

        serializer = EvalDashboardOverviewQuerySerializer(data=request.query_params)
        if not serializer.is_valid():
            return Response({'detail': '; '.join(f'{k}: {v[0]}' for k, v in serializer.errors.items())}, status=400)
        p = serializer.validated_data
        return Response(get_eval_dashboard_overview(
            days=p['days'], root_type=p['root_type'],
            dept_id=p['dept_id'], team_id=p['team_id'],
        ))


class EvalDashboardTrendView(APIView):
    """GET /api/v1/analytics/eval-dashboard/trend/?days=7&root_type=&dimension=&dept_id=&team_id=

    看板趋势线:按天聚合各维度均分。
    - dimension 为空:返回 12 维全部趋势(前端可切换显示)
    - dimension 指定:只返回该维度趋势
    组织筛选(dept_id/team_id):按提问用户归属过滤。
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request):
        from apps.analytics.serializers import EvalDashboardTrendQuerySerializer
        from apps.analytics.services.eval_dashboard_service import get_eval_dashboard_trend

        serializer = EvalDashboardTrendQuerySerializer(data=request.query_params)
        if not serializer.is_valid():
            return Response({'detail': '; '.join(f'{k}: {v[0]}' for k, v in serializer.errors.items())}, status=400)
        p = serializer.validated_data
        return Response(get_eval_dashboard_trend(
            days=p['days'], root_type=p['root_type'], dimension=p['dimension'],
            dept_id=p['dept_id'], team_id=p['team_id'],
        ))


class EvalDashboardLowScoreView(APIView):
    """GET /api/v1/analytics/eval-dashboard/low-score-qa/?days=7&limit=20&threshold=0.5&root_type=&dept_id=&team_id=

    低分对话 Top N(按 QA 均分升序)。
    返回每个 QA 的:question/answer 摘要 + 均分 + 最低维度 + 最低分 + root_type + 时间。
    前端点击展开调 qa-detail 看完整明细。
    组织筛选(dept_id/team_id):按提问用户归属过滤。
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request):
        from apps.analytics.serializers import EvalDashboardLowScoreQuerySerializer
        from apps.analytics.services.eval_dashboard_service import get_eval_dashboard_low_score

        serializer = EvalDashboardLowScoreQuerySerializer(data=request.query_params)
        if not serializer.is_valid():
            return Response({'detail': '; '.join(f'{k}: {v[0]}' for k, v in serializer.errors.items())}, status=400)
        p = serializer.validated_data
        return Response(get_eval_dashboard_low_score(
            days=p['days'], root_type=p['root_type'],
            dept_id=p['dept_id'], team_id=p['team_id'],
            limit=p['limit'], threshold=p['threshold'],
        ))


class EvalDashboardQaDetailView(APIView):
    """GET /api/v1/analytics/eval-dashboard/qa-detail/?qa_record_id=123

    单条 QA 完整明细:
    - qa: 完整对话(question/answer/root_type/created_at/user/retrieval_hits)
    - scores: 12 维 score+reason+eval_model+latency(按 4 大类顺序)
    - avg_score: 总均分
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request):
        from apps.analytics.models import MultiDimensionScore
        qa_id = request.query_params.get('qa_record_id')
        if not qa_id:
            return Response({'detail': 'qa_record_id 必填'}, status=400)
        try:
            qa_id = int(qa_id)
        except (ValueError, TypeError):
            return Response({'detail': 'qa_record_id 必须为整数'}, status=400)

        try:
            qa = QaRecord.objects.select_related('user').get(id=qa_id)
        except QaRecord.DoesNotExist:
            return Response({'detail': 'QA 不存在'}, status=404)

        scores = list(
            MultiDimensionScore.objects.filter(qa_record_id=qa_id)
            .order_by('created_at')
            .values('dimension', 'score', 'reason', 'eval_model', 'eval_latency_ms', 'eval_batch_id', 'created_at')
        )

        # 按 4 大类顺序排序(未分类的维度放最后)
        dim_order = {d: (g, i) for g, (group, dims) in enumerate(_DIMENSION_GROUPS.items()) for i, d in enumerate(dims)}
        scores.sort(key=lambda s: dim_order.get(s['dimension'], (99, 99)))

        avg_score = sum(float(s['score'] or 0) for s in scores) / max(len(scores), 1)

        return Response({
            'qa': {
                'id': qa.id,
                'question': qa.question,
                'answer': qa.answer,
                'root_type': qa.root_type,
                'answer_type': qa.answer_type,
                'created_at': qa.created_at.isoformat(),
                'user': qa.user.username if qa.user else '-',
                'retrieval_hits': qa.retrieval_hits[:10],
                'latency_total_ms': qa.latency_total_ms,
                'tokens_total': qa.tokens_prompt + qa.tokens_completion,
            },
            'scores': [
                {
                    'dimension': s['dimension'],
                    'score': round(float(s['score'] or 0), 4),
                    'reason': s['reason'],
                    'eval_model': s['eval_model'],
                    'eval_latency_ms': s['eval_latency_ms'],
                    'eval_batch_id': s['eval_batch_id'],
                    'created_at': s['created_at'].isoformat(),
                }
                for s in scores
            ],
            'avg_score': round(avg_score, 4),
        })
