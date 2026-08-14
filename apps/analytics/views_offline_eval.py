"""
analytics views - 离线评估（黄金测试集/低分回归/检索与答案评估）
"""
from loguru import logger

from django.db import models
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.permissions import CanViewAnalytics
from rag_project.config import AnalyticsConfig

# ============================================================================
# 黄金测试集管理 Views
# ============================================================================

class GoldenDatasetListView(APIView):
    """GET/POST /api/v1/analytics/golden-datasets/"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get_permissions(self):
        # POST 为写操作,需要 write 权限;GET 只需 read
        # 在 get_permissions 阶段按 HTTP 方法切换 required_perm,避免读权限用户越权创建测试集
        if self.request.method == 'POST':
            self.required_perm = 'analytics.system.write'
        else:
            self.required_perm = 'analytics.system.read'
        return super().get_permissions()

    def get(self, request):
        from apps.analytics.models import GoldenDataset
        status = request.query_params.get('status')
        dataset_type = request.query_params.get('dataset_type')
        qs = GoldenDataset.objects.all().order_by('-updated_at')
        if status:
            qs = qs.filter(status=status)
        # 支持按 dataset_type 筛选(custom / regression_low_score),前端低分回归 Tab 用
        if dataset_type:
            qs = qs.filter(dataset_type=dataset_type)
        rows = list(qs[:100].values(
            'id', 'name', 'description', 'root_type', 'status',
            'dataset_type', 'question_count', 'version',
            'created_at', 'updated_at',
        ))
        # 补充 dataset_type 的中文展示名(避免前端维护映射表)
        type_label_map = dict(GoldenDataset.DATASET_TYPE_CHOICES)
        for r in rows:
            r['dataset_type_label'] = type_label_map.get(r['dataset_type'], r['dataset_type'])
        return Response({
            'rows': rows, 'count': len(rows),
            # 移除阈值:前端据此同步低分回归说明条,避免硬编码不一致
            'suggest_remove_passes': AnalyticsConfig.low_score_regression_suggest_remove_passes(),
        })

    def post(self, request):
        from apps.analytics.services.offline_eval_service import create_golden_dataset
        name = (request.data.get('name') or '').strip()
        if not name:
            return Response({'detail': 'name 必填'}, status=400)
        root_type = (request.data.get('root_type') or 'company_doc').strip()
        description = request.data.get('description', '')
        version = request.data.get('version', 'v1')
        ds = create_golden_dataset(
            name=name, root_type=root_type,
            description=description, version=version,
            created_by_id=request.user.id,
        )
        return Response({
            'id': ds.id, 'name': ds.name, 'root_type': ds.root_type,
            'status': ds.status, 'version': ds.version,
        })


class GoldenDatasetDetailView(APIView):
    """GET/PUT/DELETE /api/v1/analytics/golden-datasets/<id>/"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.write'

    def get_permissions(self):
        # GET 为读操作,只需 read 权限;PUT/DELETE 为写操作,需要 write 权限
        # 避免原实现中 GET 也要求 write 权限,导致只有读权限的用户无法查看单个测试集
        if self.request.method == 'GET':
            self.required_perm = 'analytics.system.read'
        else:
            self.required_perm = 'analytics.system.write'
        return super().get_permissions()

    def get(self, request, ds_id):
        from apps.analytics.models import GoldenDataset, GoldenQuestion
        from apps.analytics.serializers import GoldenQuestionSerializer
        try:
            ds = GoldenDataset.objects.get(id=ds_id)
        except GoldenDataset.DoesNotExist:
            return Response({'detail': '测试集不存在'}, status=404)
        # 一次查询同时拿到 relevant_doc_count（annotate Count）和 reference_answer（select_related）
        # 避免原实现中循环 q_obj.relevant_docs.count() 触发 N+1 COUNT 查询
        questions = (
            GoldenQuestion.objects
            .filter(dataset=ds)
            .order_by('order')
            .select_related('reference_answer')
            .annotate(relevant_doc_count=models.Count('relevant_docs'))
        )
        # 用 Serializer 替代手动循环构造 dict，字段集中管理且 relevant_doc_count/has_reference
        # 等计算字段已在序列化器中声明，便于其他接口复用
        questions_data = GoldenQuestionSerializer(questions, many=True).data
        return Response({
            'id': ds.id, 'name': ds.name, 'description': ds.description,
            'root_type': ds.root_type, 'status': ds.status,
            'dataset_type': ds.dataset_type,
            'dataset_type_label': ds.get_dataset_type_display(),
            'question_count': ds.question_count, 'version': ds.version,
            'questions': questions_data,
            # 建议移除阈值:前端据此标记"建议人工 review 移除",避免硬编码不一致
            'suggest_remove_passes': AnalyticsConfig.low_score_regression_suggest_remove_passes(),
        })

    def put(self, request, ds_id):
        from apps.analytics.models import GoldenDataset
        try:
            ds = GoldenDataset.objects.get(id=ds_id)
        except GoldenDataset.DoesNotExist:
            return Response({'detail': '测试集不存在'}, status=404)
        # 只允许更新安全字段，防止注入或类型异常
        allowed_fields = {'name', 'description', 'status', 'version'}
        for field in allowed_fields:
            if field in request.data:
                value = request.data[field]
                # name/version/description 转为字符串，status 校验是否合法
                if field == 'status' and value not in ('draft', 'active', 'archived'):
                    return Response({'detail': f'status 必须为 draft/active/archived'}, status=400)
                setattr(ds, field, str(value) if field != 'status' else value)
        ds.save()
        return Response({'id': ds.id, 'status': ds.status, 'name': ds.name})

    def delete(self, request, ds_id):
        from apps.analytics.models import GoldenDataset
        try:
            ds = GoldenDataset.objects.get(id=ds_id)
        except GoldenDataset.DoesNotExist:
            return Response({'detail': '测试集不存在'}, status=404)
        ds.delete()
        return Response({'ok': True})


class GoldenDatasetImportView(APIView):
    """POST /api/v1/analytics/golden-datasets/<id>/import/"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.write'

    def post(self, request, ds_id):
        from apps.analytics.services.offline_eval_service import import_questions_from_json
        questions_data = request.data.get('questions', [])
        if not questions_data:
            return Response({'detail': 'questions 必填'}, status=400)
        result = import_questions_from_json(
            dataset_id=ds_id,
            questions_data=questions_data,
            created_by_id=request.user.id,
        )
        return Response(result)


class GoldenDatasetExportView(APIView):
    """GET /api/v1/analytics/golden-datasets/<id>/export/"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request, ds_id):
        from apps.analytics.services.offline_eval_service import export_dataset_to_json
        data = export_dataset_to_json(ds_id)
        return Response({'dataset_id': ds_id, 'questions': data})


class GoldenQuestionView(APIView):
    """POST/DELETE /api/v1/analytics/golden-datasets/<ds_id>/questions/"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.write'

    def post(self, request, ds_id):
        from apps.analytics.services.offline_eval_service import import_questions_from_json
        q_data = {
            'question': request.data.get('question', ''),
            'question_type': request.data.get('question_type', 'factoid'),
            'difficulty': request.data.get('difficulty', 'medium'),
            'tags': request.data.get('tags', []),
            'relevant_doc_ids': request.data.get('relevant_doc_ids', []),
            'reference_answer': request.data.get('reference_answer', ''),
            'key_points': request.data.get('key_points', []),
        }
        result = import_questions_from_json(ds_id, [q_data], request.user.id)
        return Response(result)

    def delete(self, request, ds_id):
        from apps.analytics.models import GoldenQuestion
        question_id = request.query_params.get('question_id')
        if not question_id:
            return Response({'detail': 'question_id 必填'}, status=400)
        try:
            question_id = int(question_id)
        except (ValueError, TypeError):
            return Response({'detail': 'question_id 必须为整数'}, status=400)
        try:
            q = GoldenQuestion.objects.get(id=question_id, dataset_id=ds_id)
            q.delete()
            return Response({'ok': True})
        except GoldenQuestion.DoesNotExist:
            return Response({'detail': '问题不存在'}, status=404)


# ============================================================================
# 低分回归测试集 Views
# ============================================================================

class SiphonRegressionView(APIView):
    """POST /api/v1/analytics/regression/siphon/ - 手动触发低分沉淀

    从生产低分对话中取 top N 沉淀到回归测试集。同步执行(DB 操作,通常 1~2s),
    直接返回沉淀结果,前端刷新测试集列表查看新增问题。
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.write'

    def post(self, request):
        # 手动触发不受 LOW_SCORE_REGRESSION_ENABLED 开关限制
        # (开关只控制定时任务;管理员主动操作应生效)
        top_n = request.data.get('top_n')
        kwargs = {}
        if top_n:
            try:
                top_n = int(top_n)
                if top_n > 0:
                    kwargs['top_n'] = top_n
            except (ValueError, TypeError):
                return Response({'detail': 'top_n 必须为正整数'}, status=400)
        # 同步执行沉淀(DB 操作,通常 1~2s,直接返回结果避免前端轮询)
        from apps.analytics.services.regression_service import siphon_low_score_qa_to_regression_set
        try:
            result = siphon_low_score_qa_to_regression_set(**kwargs)
            return Response({'ok': True, **result})
        except Exception as e:
            logger.exception('Siphon regression failed')
            return Response({'detail': f'沉淀失败: {e}'}, status=500)


class RunRegressionEvalView(APIView):
    """POST /api/v1/analytics/regression/eval/ - 手动触发低分回归全链路评估

    对低分回归测试集执行 检索→生成→12 维评估,更新 pass_count。
    成本较高(每问题 90~180s),异步派发 Celery 任务,前端提示后刷新查看结果。

    可选参数:
    - dataset_id: 指定测试集;不传则评估所有 regression_low_score 测试集
    - limit: 每个测试集最多评估的问题数(控制单次成本)
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.write'

    def post(self, request):
        from apps.analytics.tasks import run_regression_evaluation_task
        dataset_id = request.data.get('dataset_id')
        limit = request.data.get('limit')

        kwargs = {}
        if dataset_id:
            try:
                kwargs['dataset_id'] = int(dataset_id)
            except (ValueError, TypeError):
                return Response({'detail': 'dataset_id 必须为整数'}, status=400)
        if limit:
            try:
                kwargs['limit'] = int(limit)
            except (ValueError, TypeError):
                return Response({'detail': 'limit 必须为整数'}, status=400)

        # 异步派发:全链路评估耗时取决于问题数,200 条可能 30+ 分钟
        run_regression_evaluation_task.delay(**kwargs)
        return Response({
            'ok': True, 'queued': True,
            'message': '评估已派发,全链路评估耗时较长,请稍后刷新查看 pass_count 变化',
        })


# ============================================================================
# 离线评估执行 Views
# ============================================================================

class RunRetrievalEvalView(APIView):
    """POST /api/v1/analytics/eval/retrieval/ - 执行离线检索评估"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.write'

    def post(self, request):
        from apps.analytics.services.offline_eval_service import run_retrieval_evaluation
        dataset_id = request.data.get('dataset_id')
        if not dataset_id:
            return Response({'detail': 'dataset_id 必填'}, status=400)
        try:
            dataset_id = int(dataset_id)
        except (ValueError, TypeError):
            return Response({'detail': 'dataset_id 必须为整数'}, status=400)
        try:
            report = run_retrieval_evaluation(
                dataset_id=dataset_id,
                user=request.user,
            )
            return Response({
                'ok': True,
                'report_id': report.id,
                'recall_at_5': report.recall_at_5,
                'recall_at_10': report.recall_at_10,
                'recall_at_20': report.recall_at_20,
                'mrr': report.mrr,
                'ndcg_at_10': report.ndcg_at_10,
                'questions_with_hits': report.questions_with_hits,
                'questions_without_hits': report.questions_without_hits,
            })
        except Exception as e:
            logger.exception('Retrieval eval failed')
            return Response({'detail': f'评估失败: {e}'}, status=500)


class RunAnswerEvalView(APIView):
    """POST /api/v1/analytics/eval/answer/ - 执行离线回答质量评估"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.write'

    def post(self, request):
        from apps.analytics.services.offline_eval_service import run_answer_quality_evaluation
        dataset_id = request.data.get('dataset_id')
        if not dataset_id:
            return Response({'detail': 'dataset_id 必填'}, status=400)
        try:
            dataset_id = int(dataset_id)
        except (ValueError, TypeError):
            return Response({'detail': 'dataset_id 必须为整数'}, status=400)
        try:
            max_questions = int(request.data.get('max_questions', 50))
        except (ValueError, TypeError):
            return Response({'detail': 'max_questions 必须为整数'}, status=400)
        # 限制评估数量，防止 LLM 成本失控
        max_questions = max(1, min(max_questions, 100))
        try:
            results = run_answer_quality_evaluation(
                dataset_id=int(dataset_id),
                user=request.user,
                max_questions=max_questions,
            )
            return Response({
                'ok': True,
                'evaluated_count': len(results),
                'results': results[:20],
            })
        except Exception as e:
            logger.exception('Answer eval failed')
            return Response({'detail': f'评估失败: {e}'}, status=500)


class RetrievalReportListView(APIView):
    """GET /api/v1/analytics/eval/retrieval-reports/?dataset_id=<id>

    dataset_id 可选:不传返回全部历史报告,传入则仅返回该测试集的报告
    (前端测试集下拉联动时携带,避免列表混入其他测试集报告)。
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request):
        from apps.analytics.models import RetrievalQualityReport
        qs = RetrievalQualityReport.objects.select_related('dataset').order_by('-created_at')
        # 非法 dataset_id 直接返回 400,避免类型错误干扰列表加载
        dataset_id = request.query_params.get('dataset_id')
        if dataset_id:
            try:
                dataset_id = int(dataset_id)
            except (TypeError, ValueError):
                return Response({'detail': 'dataset_id 必须为整数'}, status=400)
            qs = qs.filter(dataset_id=dataset_id)
        rows = list(qs[:50].values(
            'id', 'dataset_id', 'eval_batch_id',
            'recall_at_5', 'recall_at_10', 'recall_at_20', 'mrr', 'ndcg_at_5', 'ndcg_at_10',
            'vector_recall_at_10', 'bm25_recall_at_10', 'hybrid_recall_at_10', 'rerank_recall_at_10',
            'total_questions', 'questions_with_hits', 'questions_without_hits',
            'status', 'created_at',
        ))
        return Response({'rows': rows, 'count': len(rows)})
