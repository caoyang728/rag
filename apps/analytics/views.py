"""
analytics views - 关键词权重 & 日报 & 统计
"""
from loguru import logger
from datetime import timedelta, datetime

from django.db import models
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.models import KeywordWeight, AccuracyReport
from apps.chat.models import QaRecord, QaFeedback



class KeywordWeightListView(APIView):
    """GET /api/v1/analytics/keywords/?root_type=&top=20"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        top = int(request.query_params.get("top") or 20)
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
        keyword = request.data.get("keyword")
        weight_score = request.data.get("weight_score")
        root_type = request.data.get("root_type", "all")
        if not keyword:
            return Response({"detail": "keyword 必填"}, status=400)

        obj, created = KeywordWeight.objects.update_or_create(
            keyword=keyword, root_type=root_type,
            defaults={"weight_score": weight_score}
        )
        return Response({
            "id": obj.id, "keyword": obj.keyword, "root_type": obj.root_type,
            "weight_score": obj.weight_score, "created": created
        })


class KeywordWeightDetailView(APIView):
    """PUT /api/v1/analytics/keywords/{id}/ 调整权重"""
    permission_classes = [IsAuthenticated]

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
            kw.weight_score = max(0.1, min(2.0, kw.weight_score + delta))
            kw.save(update_fields=["weight_score"])
            logger.info(
                "keyword_weight_adjusted kw_id=%s keyword=%s old=%.2f delta=%+.2f new=%.2f user=%s",
                kw.id, kw.keyword, old_score, delta, kw.weight_score,
                request.user.username
            )
            return Response({
                "id": kw.id, "keyword": kw.keyword, "weight_score": kw.weight_score
            })
        except KeywordWeight.DoesNotExist:
            return Response({"detail": "keyword weight 不存在"}, status=404)


class DailyReportView(APIView):
    """GET /api/v1/analytics/daily/ 日报"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.utils import timezone

        today = timezone.now().date()
        yesterday = today - timedelta(days=1)

        result = {"today": None, "yesterday": None}
        for key, day in [("today", today), ("yesterday", yesterday)]:
            base = QaRecord.objects.filter(created_at__date=day)
            qa_count = base.count()
            fb_stats = QaFeedback.objects.filter(
                qa_record__created_at__date=day
            ).aggregate(
                good=models.Count("id", filter=models.Q(rating__gt=0)),
                bad=models.Count("id", filter=models.Q(rating__lt=0)),
            )
            good = fb_stats["good"] or 0
            bad = fb_stats["bad"] or 0
            result[key] = {
                "date": day.isoformat(),
                "qa_count": qa_count,
                "good": good,
                "bad": bad,
                "accuracy": round(good / max(good + bad, 1), 4),
            }

        return Response(result)


class TrendReportView(APIView):
    """GET /api/v1/analytics/trend/?days=7&root_type=
    或 GET /api/v1/analytics/trend/?start_date=2025-01-01&end_date=2025-01-15&root_type=
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.utils import timezone
        from django.db.models.functions import TruncDate

        start_date_param = request.query_params.get("start_date")
        end_date_param = request.query_params.get("end_date")
        root_type = request.query_params.get("root_type")

        if start_date_param and end_date_param:
            try:
                start_date = datetime.fromisoformat(start_date_param).date()
                end_date = datetime.fromisoformat(end_date_param).date()
            except ValueError:
                return Response({"detail": "日期格式应为 YYYY-MM-DD"}, status=400)
            if start_date > end_date:
                return Response({"detail": "开始日期不能晚于结束日期"}, status=400)
            if (end_date - start_date).days > 365:
                return Response({"detail": "自定义范围最多 365 天"}, status=400)
        else:
            days = int(request.query_params.get("days") or 7)
            end_date = timezone.now().date()
            start_date = end_date - timedelta(days=days - 1)

        # 单次 GROUP BY 查询：qa count + avg latency
        qa_qs = (QaRecord.objects
                 .filter(created_at__date__gte=start_date, created_at__date__lte=end_date)
                 .annotate(day=TruncDate("created_at"))
                 .values("day")
                 .annotate(qa_count=models.Count("id"),
                           avg_latency=models.Avg("latency_total_ms"))
                 .order_by("day"))
        if root_type:
            qa_qs = qa_qs.filter(root_type=root_type)
        qa_map = {r["day"].isoformat(): r for r in qa_qs}

        # 单次 GROUP BY 查询：good / bad feedback count
        fb_qs = (QaFeedback.objects
                 .filter(qa_record__created_at__date__gte=start_date,
                         qa_record__created_at__date__lte=end_date)
                 .annotate(day=TruncDate("qa_record__created_at"))
                 .values("day")
                 .annotate(good=models.Count("id", filter=models.Q(rating__gt=0)),
                           bad=models.Count("id", filter=models.Q(rating__lt=0)))
                 .order_by("day"))
        if root_type:
            fb_qs = fb_qs.filter(qa_record__root_type=root_type)
        fb_map = {r["day"].isoformat(): r for r in fb_qs}

        days_total = (end_date - start_date).days + 1
        trend = []
        for i in range(days_total):
            day = start_date + timedelta(days=i)
            day_str = day.isoformat()
            qa_row = qa_map.get(day_str, {})
            fb_row = fb_map.get(day_str, {})
            qa_count = qa_row.get("qa_count", 0)
            good = fb_row.get("good", 0)
            bad = fb_row.get("bad", 0)
            trend.append({
                "date": day_str,
                "qa_count": qa_count,
                "good": good,
                "bad": bad,
                "accuracy": round(good / max(good + bad, 1), 4),
                "avg_latency_ms": int(qa_row.get("avg_latency", 0) or 0),
            })

        return Response({"trend": trend, "days": len(trend)})


class BadFeedbackListView(APIView):
    """GET /api/v1/analytics/bad-feedbacks/?top=20&root_type="""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        top = int(request.query_params.get("top") or 20)
        root_type = request.query_params.get("root_type")
        qs = QaFeedback.objects.filter(rating__lt=0).select_related("qa_record", "user")
        if root_type:
            qs = qs.filter(qa_record__root_type=root_type)
        qs = qs.order_by("-created_at")[:top]
        rows = []
        for fb in qs:
            rows.append({
                "id": fb.id,
                "qa_record_id": fb.qa_record_id,
                "question": fb.qa_record.question[:100] if fb.qa_record else "",
                "answer": fb.qa_record.answer[:100] if fb.qa_record else "",
                "rating": fb.rating,
                "tags": fb.tags,
                "comment": fb.comment,
                "status": fb.status,
                "user": fb.user.real_name or fb.user.username if fb.user else "",
                "created_at": fb.created_at.isoformat() if fb.created_at else "",
            })
        return Response({"rows": rows, "count": len(rows)})


class OverviewStatsView(APIView):
    """GET /api/v1/analytics/overview/?root_type= 概览统计"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.utils import timezone
        from apps.knowledge.models import Document
        from apps.memory.models import Session

        today = timezone.now().date()
        week_ago = today - timedelta(days=7)
        root_type = request.query_params.get("root_type")

        qa_qs = QaRecord.objects.all()
        if root_type:
            qa_qs = qa_qs.filter(root_type=root_type)

        total_qa = qa_qs.count()
        weekly_qa = qa_qs.filter(created_at__date__gte=week_ago).count()

        fb_qs = QaFeedback.objects.all()
        if root_type:
            fb_qs = fb_qs.filter(qa_record__root_type=root_type)
        good_fb = fb_qs.filter(rating__gt=0).count()
        bad_fb = fb_qs.filter(rating__lt=0).count()
        accuracy = round(good_fb / max(good_fb + bad_fb, 1), 4)

        avg_latency = qa_qs.aggregate(avg=models.Avg("latency_total_ms"))["avg"] or 0

        active_users = qa_qs.filter(created_at__date__gte=week_ago).values("user").distinct().count()

        doc_qs = Document.objects.filter(is_deleted=False)
        if root_type:
            doc_qs = doc_qs.filter(root_type=root_type)
        total_docs = doc_qs.count()
        completed_docs = doc_qs.filter(status="done").count()

        active_sessions = Session.objects.filter(is_deleted=False, last_active_at__date__gte=week_ago).count()

        return Response({
            "total_qa": total_qa,
            "weekly_qa": weekly_qa,
            "accuracy": accuracy,
            "avg_latency_ms": int(avg_latency),
            "active_users": active_users,
            "total_docs": total_docs,
            "completed_docs": completed_docs,
            "active_sessions": active_sessions,
        })


class QaRecordView(APIView):
    """GET /api/v1/analytics/qa-records/?start_date=&end_date=&root_type="""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.utils import timezone
        qs = QaRecord.objects.all().select_related("feedback").order_by("-created_at")

        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        root_type = request.query_params.get("root_type")

        if start_date:
            try:
                start = datetime.fromisoformat(start_date)
                qs = qs.filter(created_at__gte=start)
            except ValueError:
                pass

        if end_date:
            try:
                end = datetime.fromisoformat(end_date)
                qs = qs.filter(created_at__lte=end)
            except ValueError:
                pass

        if root_type:
            qs = qs.filter(root_type=root_type)

        page = int(request.query_params.get("page") or 1)
        size = int(request.query_params.get("page_size") or 20)
        offset = (page - 1) * size

        total = qs.count()
        rows = []
        for r in qs[offset:offset + size]:
            try:
                feedback = r.feedback
                rating = feedback.rating
            except QaFeedback.DoesNotExist:
                rating = 0

            rows.append({
                "id": r.id,
                "question": r.question,
                "answer": r.answer,
                "answer_type": r.answer_type,
                "root_type": r.root_type,
                "rating": rating,
                "latency_total_ms": r.latency_total_ms,
                "tokens_prompt": r.tokens_prompt,
                "tokens_completion": r.tokens_completion,
                "cost_estimate": float(r.cost_estimate),
                "is_hit_cache": r.is_hit_cache,
                "created_at": r.created_at.isoformat(),
            })

        return Response({
            "total": total,
            "page": page,
            "page_size": size,
            "rows": rows,
        })


class BadFeedbackDetailView(APIView):
    """PUT /api/v1/analytics/bad-feedbacks/{id}/ 标记反馈状态"""
    permission_classes = [IsAuthenticated]

    def put(self, request, fb_id):
        status = request.data.get("status", "resolved")
        if status not in ("pending", "processing", "resolved", "ignored"):
            return Response({"detail": "无效的状态值"}, status=400)

        try:
            fb = QaFeedback.objects.get(id=fb_id)
            fb.status = status
            fb.save(update_fields=["status"])
            logger.info(
                "feedback_status_updated fb_id=%s status=%s user=%s",
                fb.id, status, request.user.username
            )
            return Response({"id": fb.id, "status": fb.status})
        except QaFeedback.DoesNotExist:
            return Response({"detail": "反馈不存在"}, status=404)