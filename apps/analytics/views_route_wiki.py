"""
analytics views - 路由分析看板 & Wiki 页面质量
"""
from loguru import logger
from datetime import timedelta

from django.db import models
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.permissions import CanViewAnalytics


class RouteAnalysisDashboardView(APIView):
    """GET /api/v1/analytics/eval-dashboard/route-analysis/?days=7&dept_id=&team_id=

    路由分析看板：四层路由命中率 + 各维均分对比（由 aggregate_route_analysis_daily 任务供数）。

    - coverage_by_route: 每层命中数/占比/平均置信度/平均延迟/平均质量分
    - quality_by_route: 各层 12 维均分对比（按 4 大类分组，柱状/雷达图用）
    - daily_trend: 按天各层命中数（命中趋势堆叠图用）
    - query_transform_stats: 查询改写/分解统计（改写命中率，从 QaRecord.route_trace 实时聚合）

    时间窗口按 qa_created_at（提问时间）过滤；组织筛选按提问用户归属子查询
    （qa_record_id 为 BigInteger 非外键，无法直接 JOIN QaRecord，用子查询收敛）。
    聚合逻辑在 services/eval_dashboard_service.get_route_analysis。
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request):
        from apps.analytics.serializers import RouteAnalysisQuerySerializer
        from apps.analytics.services.eval_dashboard_service import get_route_analysis

        serializer = RouteAnalysisQuerySerializer(data=request.query_params)
        if not serializer.is_valid():
            return Response({'detail': '; '.join(f'{k}: {v[0]}' for k, v in serializer.errors.items())}, status=400)
        p = serializer.validated_data
        return Response(get_route_analysis(
            days=p['days'], dept_id=p['dept_id'], team_id=p['team_id'],
        ))


class RouteAnalysisAggregateView(APIView):
    """POST /api/v1/analytics/route-analysis/aggregate/ - 手动触发路由分析聚合

    body: {report_date: 'YYYY-MM-DD'} 可选；缺省聚合昨天。
    异步执行：POST 派发 Celery 任务立即返回，前端轮询看板数据刷新。
    用途：每日 beat 任务之外，运营改完路由配置后可立即重跑某天数据。
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.write'

    def post(self, request):
        from apps.analytics.tasks import aggregate_route_analysis_daily

        report_date = (request.data.get('report_date') or '').strip() or None
        if report_date:
            try:
                timezone.datetime.strptime(report_date, '%Y-%m-%d')
            except ValueError:
                return Response({'detail': 'report_date 格式须为 YYYY-MM-DD'}, status=400)

        aggregate_route_analysis_daily.delay(report_date)
        logger.info(
            f'[RouteAnalysis] 手动触发聚合 report_date={report_date or "yesterday"} '
            f'user={request.user.username}'
        )
        return Response({
            'ok': True,
            'queued': True,
            'report_date': report_date or 'yesterday',
            'message': '聚合已派发，稍后刷新看板即可看到结果',
        })


# ============================================================================
# Wiki 页面质量评估（忠实度 / 完整性 LLM-as-Judge）
# ============================================================================

class WikiQualityListView(APIView):
    """GET /api/v1/analytics/wiki-quality/?days=7&dimension=&status=&limit=&offset=&page_id=

    Wiki 页面质量评估结果列表：
    - summary: 统计（评估页数 / 失败页数 / 各维均分）
    - rows: 页面粒度明细（每页两个维度的 score + status + reason 截断）
    dimension 可选 faithfulness/completeness；status 可选 completed/failed；
    days 按评估更新时间窗口过滤；page_id 精确查某页（详情弹窗用，忽略窗口）。
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request):
        from apps.analytics.models import WikiPageQualityScore

        try:
            days = int(request.query_params.get('days', 7))
        except (ValueError, TypeError):
            days = 7
        days = max(1, min(days, 90))
        dimension = (request.query_params.get('dimension') or '').strip()
        status = (request.query_params.get('status') or '').strip()
        try:
            limit = int(request.query_params.get('limit', 50))
        except (ValueError, TypeError):
            limit = 50
        limit = max(1, min(limit, 200))
        try:
            offset = int(request.query_params.get('offset', 0))
        except (ValueError, TypeError):
            offset = 0
        offset = max(0, offset)

        since = timezone.now() - timedelta(days=days)
        qs = WikiPageQualityScore.objects.select_related('page').filter(updated_at__gte=since)
        if dimension:
            qs = qs.filter(dimension=dimension)
        if status:
            qs = qs.filter(status=status)
        # 精确查某页详情时(前端详情弹窗),忽略 days 窗口直接返回该页
        page_id = request.query_params.get('page_id', '').strip()
        if page_id:
            try:
                qs = qs.filter(page_id=int(page_id))
            except (ValueError, TypeError):
                return Response({'detail': 'page_id 必须为整数'}, status=400)

        # summary：各维均分 + 评估/失败页数（一次分组 + 两次 distinct 计数）
        dim_agg = qs.values('dimension').annotate(
            avg_score=models.Avg('score'),
            cnt=models.Count('id'),
        )
        summary = {'pages_evaluated': qs.values('page_id').distinct().count()}
        for r in dim_agg:
            summary[f'avg_{r["dimension"]}'] = round(float(r['avg_score'] or 0), 4)
            summary[f'count_{r["dimension"]}'] = r['cnt']
        summary['failed_pages'] = (
            qs.filter(status='failed').values('page_id').distinct().count()
        )

        # 明细：取窗口内 page_id 分页，再整批取每页两维分数（避免逐页 N+1）
        page_ids = list(
            qs.order_by('-updated_at').values_list('page_id', flat=True).distinct()[offset:offset + limit]
        )
        page_rows = {}
        if page_ids:
            score_rows = WikiPageQualityScore.objects.select_related('page').filter(
                page_id__in=page_ids,
            ).order_by('-updated_at')
            for s in score_rows:
                pm = page_rows.setdefault(s.page_id, {
                    'page_id': s.page_id,
                    'title': s.page.title,
                    'node_id': s.page.node_id,
                    'scores': {},
                })
                pm['scores'][s.dimension] = {
                    'score': round(float(s.score or 0), 4),
                    'status': s.status,
                    'reason': s.reason[:200],
                    'error_message': s.error_message[:200],
                    'updated_at': s.updated_at.isoformat(),
                }
        rows = list(page_rows.values())

        return Response({
            'days': days,
            'dimension': dimension or 'all',
            'status': status or 'all',
            'total': len(rows),
            'offset': offset,
            'limit': limit,
            'summary': summary,
            'rows': rows,
        })


class WikiQualityEvaluateView(APIView):
    """POST /api/v1/analytics/wiki-quality/evaluate/ - 手动触发 Wiki 页面质量批量评估

    body: {days: 7, limit: N} 可选；days 控制只重评估近期更新页面，limit 限制单次页面数。
    异步执行：POST 派发 Celery 任务立即返回，前端轮询 wiki-quality 列表查结果。
    目的：发布新 Wiki 或更新源文档后可手动触发，不必等每日 beat 任务。
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.write'

    def post(self, request):
        from apps.analytics.tasks import batch_evaluate_wiki_quality

        days = 7
        if request.data.get('days') is not None:
            try:
                days = int(request.data.get('days'))
            except (ValueError, TypeError):
                return Response({'detail': 'days 必须为整数'}, status=400)
            days = max(1, min(days, 90))

        limit = None
        if request.data.get('limit') is not None:
            try:
                limit = int(request.data.get('limit'))
            except (ValueError, TypeError):
                return Response({'detail': 'limit 必须为整数'}, status=400)
            limit = max(1, min(limit, 500))

        batch_evaluate_wiki_quality.delay(days=days, limit=limit)
        logger.info(
            f'[WikiEval] 手动触发批量评估 days={days} limit={limit} user={request.user.username}'
        )
        return Response({
            'ok': True,
            'queued': True,
            'days': days,
            'limit': limit,
            'message': '评估已派发，稍后刷新列表即可看到结果',
        })
