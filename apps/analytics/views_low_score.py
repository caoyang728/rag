"""
analytics views - 低分对话归因分析
"""
from loguru import logger
from datetime import timedelta

from django.db import models
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.views_common import _parse_org_scope, _apply_org_filter_on_qa
from apps.chat.models import QaRecord
from apps.users.permissions import CanViewAnalytics

# ============================================================================
# 低分归因分析 Views
# ============================================================================

class LowScoreAnalysisListView(APIView):
    """GET /api/v1/analytics/low-score-analysis/?days=7&category=&layer=&status=&root_type=&dept_id=&team_id=&limit=50

    低分归因列表:
    - 支持时间窗口、归因分类、影响层级、状态、组织归属筛选
    - select_related('qa_record') 避免 N+1(serializer 取 question/answer/root_type)
    - 默认按创建时间倒序,limit 上限 200 防止过大响应
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request):
        from apps.analytics.models import LowScoreAnalysis
        from apps.analytics.serializers import LowScoreAnalysisSerializer

        # 时间窗口
        try:
            days = int(request.query_params.get('days', 7))
        except (ValueError, TypeError):
            return Response({'detail': 'days 必须为整数'}, status=400)
        days = max(1, min(days, 90))
        since = timezone.now() - timedelta(days=days)

        try:
            limit = int(request.query_params.get('limit', 50))
        except (ValueError, TypeError):
            return Response({'detail': 'limit 必须为整数'}, status=400)
        limit = max(1, min(limit, 200))

        category = request.query_params.get('category', '').strip()
        layer = request.query_params.get('layer', '').strip()
        status = request.query_params.get('status', '').strip()
        root_type = request.query_params.get('root_type', '').strip()
        dept_id, team_id = _parse_org_scope(request)

        qs = LowScoreAnalysis.objects.select_related('qa_record').filter(created_at__gte=since)
        if category:
            qs = qs.filter(root_cause_category=category)
        if layer:
            qs = qs.filter(affected_layer=layer)
        if status:
            qs = qs.filter(status=status)
        if root_type:
            qs = qs.filter(qa_record__root_type=root_type)
        # 组织筛选:按 QaRecord.user 的归属 JOIN,LowScoreAnalysis 与 qa_record__user 关联
        qs = _apply_org_filter_on_qa(qs, dept_id, team_id, qa_prefix='qa_record__')

        qs = qs.order_by('-created_at')[:limit]
        rows = LowScoreAnalysisSerializer(qs, many=True).data
        return Response({'rows': rows, 'count': len(rows), 'days': days,
                         'dept_id': dept_id, 'team_id': team_id})


class LowScoreAnalysisDetailView(APIView):
    """GET /api/v1/analytics/low-score-analysis/?qa_record_id=123

    单条 QA 的归因详情(前端点击列表行展开用)。
    返回完整 question/answer + 归因结论 + 建议 + 低分维度 reason。
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request):
        from apps.analytics.models import LowScoreAnalysis
        from apps.analytics.serializers import LowScoreAnalysisSerializer

        qa_id = request.query_params.get('qa_record_id')
        if not qa_id:
            return Response({'detail': 'qa_record_id 必填'}, status=400)
        try:
            qa_id = int(qa_id)
        except (ValueError, TypeError):
            return Response({'detail': 'qa_record_id 必须为整数'}, status=400)

        try:
            analysis = LowScoreAnalysis.objects.select_related('qa_record').get(qa_record_id=qa_id)
        except LowScoreAnalysis.DoesNotExist:
            return Response({'detail': '该 QA 暂无归因分析,请先触发评估与归因'}, status=404)

        data = LowScoreAnalysisSerializer(analysis).data
        # 详情接口补充完整对话内容(列表已截断)
        data['full_question'] = analysis.qa_record.question if analysis.qa_record else ''
        data['full_answer'] = analysis.qa_record.answer if analysis.qa_record else ''
        return Response(data)


class RunLowScoreAnalysisView(APIView):
    """POST /api/v1/analytics/low-score-analysis/run/ - 手动触发单条 QA 归因

    场景:运营在看板发现某低分 QA,手动触发归因(跳过日预算,用户主动操作不计入生产配额)。
    异步执行:POST 立即返回,前端轮询 detail 接口查结果。
    若该 QA 尚未评估(无 MultiDimensionScore),返回 400 提示先评估。
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.write'

    def post(self, request):
        from apps.analytics.tasks import run_low_score_analysis
        from apps.analytics.models import MultiDimensionScore

        qa_id = request.data.get('qa_record_id')
        if not qa_id:
            return Response({'detail': 'qa_record_id 必填'}, status=400)
        try:
            qa_id = int(qa_id)
        except (ValueError, TypeError):
            return Response({'detail': 'qa_record_id 必须为整数'}, status=400)

        # 预检:无评估分数的 QA 无法归因,提前返回避免无意义排队
        has_scores = MultiDimensionScore.objects.filter(qa_record_id=qa_id).exists()
        if not has_scores:
            return Response({'detail': '该 QA 尚未评估,请先在「回答质量」执行 12 维评估'}, status=400)

        # 存在性预检(无需取回完整对象,存在与否即可)
        if not QaRecord.objects.filter(id=qa_id).exists():
            return Response({'detail': 'QA 记录不存在'}, status=404)

        threshold = request.data.get('threshold')
        if threshold is not None:
            try:
                threshold = float(threshold)
            except (TypeError, ValueError):
                return Response({'detail': 'threshold 必须为数字'}, status=400)

        # 派发异步归因任务(手动触发跳过日预算)
        run_low_score_analysis.delay(qa_id, threshold=threshold, skip_budget_check=True)
        logger.info(f'[LowScoreAnalysis] 手动触发归因 qa_id={qa_id} user={request.user.username}')

        return Response({
            'ok': True,
            'queued': True,
            'qa_id': qa_id,
            'message': '归因已派发,请通过 qa_record_id 轮询结果',
        })


class LowScoreAnalysisStatsView(APIView):
    """GET /api/v1/analytics/low-score-analysis/stats/?days=7&root_type=&dept_id=&team_id=

    归因分类统计(前端归因分布图用):
    - by_category: [{category, count, avg_score}]
    - by_layer: [{layer, count}]
    - by_method: {rule: n, llm: n, hybrid: n}
    - total: 总归因数
    一次 GROUP BY 查询拿全,避免逐类 COUNT
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request):
        from apps.analytics.models import LowScoreAnalysis

        try:
            days = int(request.query_params.get('days', 7))
        except (ValueError, TypeError):
            days = 7
        days = max(1, min(days, 90))
        root_type = request.query_params.get('root_type', '').strip()
        dept_id, team_id = _parse_org_scope(request)

        since = timezone.now() - timedelta(days=days)
        qs = LowScoreAnalysis.objects.filter(created_at__gte=since, status='completed')
        if root_type:
            qs = qs.filter(qa_record__root_type=root_type)
        qs = _apply_org_filter_on_qa(qs, dept_id, team_id, qa_prefix='qa_record__')

        # 按分类聚合:count + avg_score(一次 GROUP BY)
        cat_agg = qs.values('root_cause_category').annotate(
            count=models.Count('id'),
            avg_score=models.Avg('avg_score'),
        ).order_by('-count')
        by_category = [
            {
                'category': r['root_cause_category'],
                'count': r['count'],
                'avg_score': round(float(r['avg_score'] or 0), 4),
            }
            for r in cat_agg
        ]

        # 按层级聚合
        layer_agg = qs.values('affected_layer').annotate(
            count=models.Count('id'),
        ).order_by('-count')
        by_layer = [{'layer': r['affected_layer'], 'count': r['count']} for r in layer_agg]

        # 按方法聚合
        method_agg = qs.values('analysis_method').annotate(count=models.Count('id'))
        by_method = {r['analysis_method']: r['count'] for r in method_agg}

        total = sum(r['count'] for r in by_category)

        return Response({
            'days': days,
            'root_type': root_type or 'all',
            'dept_id': dept_id,
            'team_id': team_id,
            'total': total,
            'by_category': by_category,
            'by_layer': by_layer,
            'by_method': {
                'rule': by_method.get('rule', 0),
                'llm': by_method.get('llm', 0),
                'hybrid': by_method.get('hybrid', 0),
            },
        })
