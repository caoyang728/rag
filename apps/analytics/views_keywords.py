"""
analytics views - 关键词权重 & 检索反馈闭环
"""
from loguru import logger
from datetime import datetime

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.models import (
    KeywordWeight, ChunkClickLog, KeywordFeedbackAgg,
)
from apps.users.permissions import CanViewAnalytics

class KeywordWeightListView(APIView):
    """GET /api/v1/analytics/keywords/?root_type=&top=20

    - 关键词权重列表，管理员可查看
    - top 参数限制 1-500，防止恶意拉取全表
    - POST 时校验 keyword 非空 + weight_score 范围 0.1~5.0
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request):
        try:
            top = int(request.query_params.get("top") or 20)
        except (ValueError, TypeError):
            return Response({"detail": "top 必须为整数"}, status=400)
        # top 上限 500，防止 OOM / 全表扫描
        top = max(1, min(top, 500))
        root_type = request.query_params.get("root_type")
        qs = KeywordWeight.objects.all().order_by("-weight_score")
        if root_type:
            qs = qs.filter(root_type=root_type)
        rows = list(qs[:top].values(
            "id", "keyword", "root_type", "hit_count", "good_feedback", "bad_feedback",
            "weight_score", "updated_at"
        ))
        return Response({"rows": rows, "count": len(rows)})

    def post(self, request):
        keyword = (request.data.get("keyword") or "").strip()
        weight_score = request.data.get("weight_score")
        # root_type 长度与模型 CharField(max_length=32) 对齐；
        # 超长时在 API 层直接返回 400，避免 DB 层 DataError 或静默截断
        root_type = (request.data.get("root_type", "all") or "").strip() or "all"
        if not keyword:
            return Response({"detail": "keyword 必填"}, status=400)
        if len(keyword) > 64:
            return Response({"detail": "keyword 长度不能超过 64 字符"}, status=400)
        if len(root_type) > 32:
            return Response({"detail": "root_type 长度不能超过 32 字符"}, status=400)
        # weight_score 防御性钳位：未传默认 1.0，范围 0.1~5.0
        try:
            weight_score = float(weight_score) if weight_score is not None else 1.0
        except (TypeError, ValueError):
            return Response({"detail": "weight_score 必须为数字"}, status=400)
        weight_score = max(0.1, min(5.0, weight_score))

        obj, created = KeywordWeight.objects.update_or_create(
            keyword=keyword, root_type=root_type,
            defaults={"weight_score": weight_score}
        )
        return Response({
            "id": obj.id, "keyword": obj.keyword, "root_type": obj.root_type,
            "weight_score": obj.weight_score, "created": created
        })


class KeywordWeightDetailView(APIView):
    """PUT /api/v1/analytics/keywords/{id}/ 调整权重

    - 修改关键词权重直接影响检索排序（weight_score 范围 0.1~5.0，与创建接口一致）
    - 仅限管理员操作，需记录审计日志
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.write'

    def put(self, request, kw_id):
        delta = request.data.get("delta")
        if delta is None:
            return Response({"detail": "delta 必填"}, status=400)
        try:
            delta = float(delta)
        except (TypeError, ValueError):
            return Response({"detail": "delta 必须为数字"}, status=400)

        try:
            kw = KeywordWeight.objects.get(id=kw_id)
            old_score = kw.weight_score
            # 上限与创建接口（0.1~5.0）保持一致，避免高权重关键词 +0.1 被静默压回 2.0
            kw.weight_score = max(0.1, min(5.0, kw.weight_score + delta))
            kw.save(update_fields=["weight_score"])
            # 手动调整同样落 KeywordFeedbackAgg，与自动调整统一审计展示；
            # 同 (日期, 关键词) 自动任务将跳过应用，保证手动覆盖优先
            from apps.analytics.services.feedback_service import record_manual_adjustment
            record_manual_adjustment(kw, old_score, request.user)
            logger.info(
                f"keyword_weight_adjusted kw_id={kw.id} keyword={kw.keyword} old={old_score:.2f} delta={delta:+.2f} new={kw.weight_score:.2f} user={request.user.username}"
            )
            return Response({
                "id": kw.id, "keyword": kw.keyword, "weight_score": kw.weight_score
            })
        except KeywordWeight.DoesNotExist:
            return Response({"detail": "keyword weight 不存在"}, status=404)


class ChunkClickLogView(APIView):
    """POST /api/v1/analytics/chunk-clicks/ - 记录一次溯源来源点击

    前端在用户点击回答的引用卡片时调用（聊天页 previewCitation 埋点），
    作为反馈闭环"点击率"的原始数据源。点击是低频行为，直接 INSERT 落库。

    请求体：
        chunk_id: int 必填（被点击的 chunk id）
        qa_record_id: int 可选（本次回答的 QaRecord.id，聚合点击率时需要）
        document_id: int 可选
        root_type: str 可选（默认 all）
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            chunk_id = int(request.data.get("chunk_id"))
        except (TypeError, ValueError):
            return Response({"detail": "chunk_id 必填且为整数"}, status=400)
        if chunk_id <= 0:
            return Response({"detail": "chunk_id 必须为正整数"}, status=400)

        qa_record_id = request.data.get("qa_record_id")
        try:
            qa_record_id = int(qa_record_id) if qa_record_id not in (None, "") else None
        except (TypeError, ValueError):
            qa_record_id = None
        document_id = request.data.get("document_id")
        try:
            document_id = int(document_id) if document_id not in (None, "") else None
        except (TypeError, ValueError):
            document_id = None
        root_type = (request.data.get("root_type") or "all").strip() or "all"
        # root_type 长度与模型 CharField(max_length=32) 对齐，超长直接 400，
        # 否则会触发 DB DataError 导致 500（与 KeywordWeightListView.post 同一口径）
        if len(root_type) > 32:
            return Response({"detail": "root_type 长度不能超过 32 字符"}, status=400)

        # 记录点击日志；qa_record/user 以请求携带为准，写入失败不阻塞前端
        ChunkClickLog.objects.create(
            user=request.user,
            qa_record_id=qa_record_id,
            document_id=document_id,
            chunk_id=chunk_id,
            root_type=root_type,
        )
        return Response({"ok": True})


class KeywordFeedbackAggListView(APIView):
    """GET /api/v1/analytics/feedback-loop/aggregations/ - 自动调整记录列表

    支持 date/keyword/root_type/status 筛选，limit 上限 100，
    供运营工具展示反馈闭环的每日聚合结果与权重调整审计。
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request):
        from apps.analytics.serializers import KeywordFeedbackAggSerializer

        qs = KeywordFeedbackAgg.objects.all()
        date = (request.query_params.get("date") or "").strip()
        if date:
            # 先解析校验日期格式，避免非法字符串直接透传到 ORM 过滤导致 500
            try:
                report_date = datetime.fromisoformat(date).date()
            except ValueError:
                return Response({"detail": "date 格式应为 YYYY-MM-DD"}, status=400)
            qs = qs.filter(report_date=report_date)
        keyword = (request.query_params.get("keyword") or "").strip()
        if keyword:
            qs = qs.filter(keyword__icontains=keyword)
        root_type = (request.query_params.get("root_type") or "").strip()
        if root_type:
            qs = qs.filter(root_type=root_type)
        status = (request.query_params.get("status") or "").strip()
        if status:
            qs = qs.filter(status=status)
        try:
            limit = max(1, min(int(request.query_params.get("limit") or 50), 100))
        except (ValueError, TypeError):
            limit = 50

        rows = KeywordFeedbackAggSerializer(
            qs.order_by("-report_date", "-created_at")[:limit], many=True
        ).data
        return Response({"rows": rows, "count": len(rows)})


class KeywordFeedbackApplyView(APIView):
    """POST /api/v1/analytics/feedback-loop/apply/ - 人工复核应用/忽略待调整记录

    请求体：{id: int, action: 'apply'|'ignore'}
    人工复核开关(AUTO_APPLY=False)产生的 pending 记录由运营在此确认后生效。
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.write'

    def post(self, request):
        from apps.analytics.services.feedback_service import apply_pending_adjustment
        try:
            agg_id = int(request.data.get("id"))
        except (TypeError, ValueError):
            return Response({"detail": "id 必填且为整数"}, status=400)
        action = (request.data.get("action") or "apply").strip()
        if action not in ("apply", "ignore"):
            return Response({"detail": "action 必须为 apply 或 ignore"}, status=400)

        ok, message = apply_pending_adjustment(agg_id, action=action, user=request.user)
        if not ok:
            return Response({"detail": message}, status=400)
        return Response({"ok": True, "detail": message})


class RunFeedbackLoopView(APIView):
    """POST /api/v1/analytics/feedback-loop/run/ - 手动触发反馈闭环聚合

    请求体：{date: 'YYYY-MM-DD' 可选}，默认聚合昨天。
    与每日定时任务共用同一逻辑，支持回补指定日期。
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.write'

    def post(self, request):
        from apps.analytics.services.feedback_service import run_keyword_feedback_loop
        date = request.data.get("date") or None
        try:
            result = run_keyword_feedback_loop(report_date=date)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        return Response(result)
