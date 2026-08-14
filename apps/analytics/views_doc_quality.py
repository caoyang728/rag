"""
analytics views - 文档质量 & 多维度评估
"""
from datetime import datetime

from django.db import models
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.views_common import _parse_org_scope, _apply_org_filter_on_doc
from apps.chat.models import QaRecord
from apps.users.permissions import CanViewAnalytics

# ============================================================================
# 文档质量 Views
# ============================================================================

class DocumentQualityReportView(APIView):
    """GET /api/v1/analytics/doc-quality/?start_date=&end_date=&dept_id=&team_id= - 文档质量汇总"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request):
        from apps.analytics.services.doc_quality_service import get_document_quality_summary
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        root_type = request.query_params.get('root_type')
        dept_id, team_id = _parse_org_scope(request)
        if start_date:
            try:
                datetime.fromisoformat(start_date)
            except ValueError:
                return Response({'detail': 'start_date 格式应为 YYYY-MM-DD'}, status=400)
        if end_date:
            try:
                datetime.fromisoformat(end_date)
            except ValueError:
                return Response({'detail': 'end_date 格式应为 YYYY-MM-DD'}, status=400)
        return Response({
            **get_document_quality_summary(
                start_date, end_date, root_type,
                dept_id=dept_id, team_id=team_id,
            ),
            'dept_id': dept_id,
            'team_id': team_id,
        })


class RunDocQualityEvalView(APIView):
    """POST /api/v1/analytics/doc-quality/evaluate/ - 触发文档质量评估"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.write'

    def post(self, request):
        from apps.analytics.services.doc_quality_service import evaluate_document_quality, batch_evaluate_document_quality
        doc_id = request.data.get('document_id')
        if doc_id:
            try:
                doc_id = int(doc_id)
            except (ValueError, TypeError):
                return Response({'detail': 'document_id 必须为整数'}, status=400)
            report = evaluate_document_quality(doc_id)
            return Response({'ok': True, 'report_id': report.id, 'score': report.quality_score})
        try:
            days = int(request.data.get('days', 7))
        except (ValueError, TypeError):
            return Response({'detail': 'days 必须为整数'}, status=400)
        days = max(1, min(days, 30))
        summary = batch_evaluate_document_quality(days=days)
        return Response({'ok': True, 'summary': summary})


class DocumentQualityReportListView(APIView):
    """GET /api/v1/analytics/doc-quality/reports/?dept_id=&team_id="""
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request):
        from apps.analytics.models import DocumentQualityReport
        from apps.analytics.serializers import DocumentQualityReportSerializer
        root_type = request.query_params.get('root_type')
        min_score = request.query_params.get('min_score')
        dept_id, team_id = _parse_org_scope(request)

        qs = DocumentQualityReport.objects.select_related('document').order_by('-created_at')
        if root_type:
            qs = qs.filter(document__root_type=root_type)
        qs = _apply_org_filter_on_doc(qs, dept_id, team_id, doc_prefix='document__')
        if min_score:
            try:
                qs = qs.filter(quality_score__gte=float(min_score))
            except (ValueError, TypeError):
                return Response({'detail': 'min_score 必须为数字'}, status=400)

        total = qs.count()
        rows = DocumentQualityReportSerializer(qs[:50], many=True).data
        return Response({'total': total, 'rows': rows, 'dept_id': dept_id, 'team_id': team_id})


# ============================================================================
# 多维度评估 Views
# ============================================================================

class MultiDimensionScoreView(APIView):
    """GET /api/v1/analytics/multi-dim-scores/ - 多维度评估结果"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request):
        from apps.analytics.models import MultiDimensionScore
        qa_id = request.query_params.get('qa_record_id')
        dimension = request.query_params.get('dimension')
        start_date = request.query_params.get('start_date')

        qs = MultiDimensionScore.objects.select_related('qa_record').order_by('-created_at')
        if qa_id:
            try:
                qs = qs.filter(qa_record_id=int(qa_id))
            except (ValueError, TypeError):
                return Response({'detail': 'qa_record_id 必须为整数'}, status=400)
        if dimension:
            qs = qs.filter(dimension=dimension)
        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date).date()
                qs = qs.filter(created_at__date__gte=start_dt)
            except ValueError:
                return Response({'detail': 'start_date 格式应为 YYYY-MM-DD'}, status=400)

        # 汇总：用一次 GROUP BY 替代原 N 次逐维度查询，避免 N+1
        dim_agg_qs = qs.values('dimension').annotate(
            count=models.Count('id'),
            avg_score=models.Avg('score'),
            min_score=models.Min('score'),
            max_score=models.Max('score'),
        )
        dim_agg = {}
        for r in dim_agg_qs:
            dim_agg[r['dimension']] = {
                'count': r['count'],
                'avg_score': round(r['avg_score'] or 0, 4),
                'min_score': round(r['min_score'] or 0, 4),
                'max_score': round(r['max_score'] or 0, 4),
            }

        total = qs.count()
        rows = list(qs[:50].values(
            'id', 'qa_record_id', 'dimension', 'score', 'reason',
            'eval_model', 'eval_cost', 'status', 'created_at',
        ))
        return Response({'total': total, 'dimension_summary': dim_agg, 'rows': rows})


class RunMultiDimEvalView(APIView):
    """POST /api/v1/analytics/multi-dim-eval/ - 手动触发单条 QA 的 DeepEval 12 维评估

    用于看板中"手动评估指定 QA"场景:运营发现某条对话异常,手动触发评估。
    实际启用维度由 EVAL_DISPLAY_DIMENSIONS 控制(默认 12 维,评估=展示强绑定)。

    异步执行:POST 立即派发 Celery 任务并返回 eval_batch_id,前端通过轮询
    qa-detail 接口检查该 batch_id 的评估结果是否落库。
    改为异步的原因:12 维 LLM 评估串行耗时 90~180s+,同步 HTTP 请求易被
    网关/浏览器超时断开,且会阻塞 Django dev server 其他请求。
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.write'

    def post(self, request):
        from apps.analytics.services.production_eval_service import evaluate_sampled_qa

        qa_id = request.data.get('qa_record_id')
        if not qa_id:
            return Response({'detail': 'qa_record_id 必填'}, status=400)
        try:
            qa_id = int(qa_id)
        except (ValueError, TypeError):
            return Response({'detail': 'qa_record_id 必须为整数'}, status=400)

        try:
            qa = QaRecord.objects.get(id=qa_id)
        except QaRecord.DoesNotExist:
            return Response({'detail': 'QA记录不存在'}, status=404)

        # 预检:无检索上下文的 QA 无法评估,提前返回避免无意义排队
        from apps.analytics.services.production_eval_service import build_context_list
        contexts = build_context_list(qa)
        if not contexts:
            return Response({'detail': '该 QA 无检索上下文,无法评估'}, status=400)

        # 预生成 eval_batch_id 作为本次评估的唯一标识
        # 前端通过该 id 在 qa-detail 接口轮询本次评估落库的维度数
        eval_batch_id = f'manual_{timezone.now().strftime("%Y%m%d%H%M%S")}_{qa_id}'

        # 派发 Celery 任务:手动评估场景跳过日预算检查(不计入生产配额)
        evaluate_sampled_qa.delay(
            qa_id,
            skip_budget_check=True,
            eval_batch_id=eval_batch_id,
        )

        return Response({
            'ok': True,
            'queued': True,
            'qa_id': qa_id,
            'eval_batch_id': eval_batch_id,
            'message': '评估已派发,请通过 eval_batch_id 轮询结果',
        })
