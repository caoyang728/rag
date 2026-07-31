"""
analytics views - 关键词权重 & 日报 & 统计 & 系统指标 & 组织报表 & 队列监控
"""
import io
import csv

from loguru import logger
from datetime import timedelta, datetime

from django.db import models
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.models import (
    KeywordWeight, SystemMetricsReport,
    OrgUsageReport, AnswerQualityReport,
)
from apps.chat.models import QaRecord, QaFeedback
from apps.users.permissions import CanViewAnalytics



class KeywordWeightListView(APIView):
    """GET /api/v1/analytics/keywords/?root_type=&top=20

    - 关键词权重列表，管理员可查看
    - top 参数限制 1-500，防止恶意拉取全表
    - POST 时校验 keyword 非空 + weight_score 范围 0.1~5.0
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics:system:read'

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

    - 修改关键词权重直接影响检索排序（weight_score 范围 0.1~2.0）
    - 仅限管理员操作，需记录审计日志
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics:system:write'

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
    """GET /api/v1/analytics/daily/ 日报
    - 展示最近 2 天（今日/昨日）的 QA 概览 + 反馈统计
    - 使用条件聚合一次性查出 qa_count/good/bad，避免 3 次 COUNT 查询
    - 仅聚合实时数据（T+1 精确指标请使用 SystemMetricsReport）
    - 支持 root_type 参数（知识库类型过滤），与前端 loadDailyReport 传参对齐
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics:system:read'

    def get(self, request):
        from django.db.models.functions import TruncDate

        today = timezone.now().date()
        yesterday = today - timedelta(days=1)
        root_type = request.query_params.get("root_type")

        # —— 1. 按日条件聚合 QaRecord 数量（一次查询出 2 天的 qa_count）——
        qa_qs = (QaRecord.objects
                 .filter(created_at__date__in=[today, yesterday]))
        if root_type:
            qa_qs = qa_qs.filter(root_type=root_type)
        qa_qs = (qa_qs
                 .annotate(day=TruncDate('created_at'))
                 .values('day')
                 .annotate(qa_count=models.Count('id')))
        qa_map = {str(r['day']): r['qa_count'] for r in qa_qs}

        # —— 2. 按日条件聚合 好评/差评（一次查询出 2 天，good+bad 同时出）——
        fb_qs = (QaFeedback.objects
                 .filter(qa_record__created_at__date__in=[today, yesterday]))
        if root_type:
            fb_qs = fb_qs.filter(qa_record__root_type=root_type)
        fb_qs = (fb_qs
                 .annotate(day=TruncDate('qa_record__created_at'))
                 .values('day')
                 .annotate(
                     good=models.Count('id', filter=models.Q(rating__gt=0)),
                     bad=models.Count('id', filter=models.Q(rating__lt=0)),
                 ))
        fb_map = {str(r['day']): {'good': r['good'] or 0, 'bad': r['bad'] or 0} for r in fb_qs}

        result = {"today": None, "yesterday": None}
        for key, day in [("today", today), ("yesterday", yesterday)]:
            day_str = day.isoformat()
            qa_count = qa_map.get(day_str, 0)
            fb = fb_map.get(day_str, {'good': 0, 'bad': 0})
            good, bad = fb['good'], fb['bad']
            result[key] = {
                "date": day_str,
                "qa_count": qa_count,
                "good": good,
                "bad": bad,
                "accuracy": round(good / max(good + bad, 1), 4),
            }

        return Response(result)


class TrendReportView(APIView):
    """GET /api/v1/analytics/trend/?days=7&root_type=
    或 GET /api/v1/analytics/trend/?start_date=2025-01-01&end_date=2025-01-15&root_type=
    - 按日汇总 QA 次数、Token、费用、准确率等趋势
    - 系统级统计，需具备 analytics:system:read 权限
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics:system:read'

    def get(self, request):
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
            # days 参数防御：非整数返回 400，范围限制 1-365 防止过大循环
            try:
                days = int(request.query_params.get("days") or 7)
            except (ValueError, TypeError):
                return Response({"detail": "days 必须为整数"}, status=400)
            if days < 1 or days > 365:
                return Response({"detail": "days 范围应为 1-365"}, status=400)
            end_date = timezone.now().date()
            start_date = end_date - timedelta(days=days - 1)

        # 构造 QaRecord 基础过滤 QS，先不要调用 values()，方便后续复用两次聚合
        qa_base_qs = QaRecord.objects.filter(
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
        )
        if root_type:
            qa_base_qs = qa_base_qs.filter(root_type=root_type)

        # 聚合 1：qa count（全量，包含缓存命中）
        qa_counts = (qa_base_qs
                      .annotate(day=TruncDate("created_at"))
                      .values("day")
                      .annotate(qa_count=models.Count("id"))
                      .order_by("day"))
        qa_map = {r["day"].isoformat(): {"qa_count": r["qa_count"]} for r in qa_counts}

        # 聚合 2：仅非缓存命中的平均耗时，分别计算首字(TTFT)和整体(total)耗时
        # —— 缓存命中请求走旁路，没有真实 LLM 生成过程，latency 代表纯读库速度，会拉低真实生成耗时
        latency_by_day = (qa_base_qs
                           .exclude(is_hit_cache=True)
                           .annotate(day=TruncDate("created_at"))
                           .values("day")
                           .annotate(avg_total=models.Avg("latency_total_ms"),
                                      avg_ttfb=models.Avg("latency_ttfb_ms"))
                           .order_by("day"))
        lat_map = {r["day"].isoformat(): r for r in latency_by_day}

        # 单次 GROUP BY 查询：good / bad feedback count
        # —— 注意：所有 filter（尤其是 JOIN 条件如 qa_record__root_type）必须放在 .values().annotate() 之前 ——
        #    否则如果 GROUP BY 已经执行完，再加 JOIN filter 会导致 Django 生成 subquery，
        #    不同版本行为不一致。提前 filter 也能减少 GROUP BY 需要处理的行数。
        fb_base = QaFeedback.objects.filter(
            qa_record__created_at__date__gte=start_date,
            qa_record__created_at__date__lte=end_date,
        )
        if root_type:
            fb_base = fb_base.filter(qa_record__root_type=root_type)
        fb_qs = (fb_base
                 .annotate(day=TruncDate("qa_record__created_at"))
                 .values("day")
                 .annotate(good=models.Count("id", filter=models.Q(rating__gt=0)),
                           bad=models.Count("id", filter=models.Q(rating__lt=0)))
                 .order_by("day"))
        fb_map = {r["day"].isoformat(): r for r in fb_qs}

        days_total = (end_date - start_date).days + 1
        trend = []
        for i in range(days_total):
            day = start_date + timedelta(days=i)
            day_str = day.isoformat()
            qa_row = qa_map.get(day_str, {})
            lat_row = lat_map.get(day_str, {})
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
                # 仅非缓存命中的真实生成耗时，缓存命中延迟仅代表纯读库速度不计入
                "avg_ttft_ms": int(lat_row.get("avg_ttfb", 0) or 0),
                "avg_total_ms": int(lat_row.get("avg_total", 0) or 0),
            })

        return Response({"trend": trend, "days": len(trend)})


class BadFeedbackListView(APIView):
    """GET /api/v1/analytics/bad-feedbacks/?top=20&root_type=

    - 差评反馈列表，仅管理员可查看
    - top 参数限制 1-500，防止恶意拉取全表
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics:system:read'

    def get(self, request):
        try:
            top = int(request.query_params.get("top") or 20)
        except (ValueError, TypeError):
            return Response({"detail": "top 必须为整数"}, status=400)
        # top 上限 500，防止 OOM
        top = max(1, min(top, 500))
        root_type = request.query_params.get("root_type")
        qs = QaFeedback.objects.filter(rating__lt=0).select_related("qa_record", "user")
        if root_type:
            qs = qs.filter(qa_record__root_type=root_type)
        qs = qs.order_by("-created_at")[:top]
        rows = []
        for fb in qs:
            # question/answer 防御性安全取值：字段本身非空时才切片，None 或空字符串安全降级
            question_val = fb.qa_record.question if (fb.qa_record and fb.qa_record.question is not None) else ""
            answer_val = fb.qa_record.answer if (fb.qa_record and fb.qa_record.answer is not None) else ""
            rows.append({
                "id": fb.id,
                "qa_record_id": fb.qa_record_id,
                "question": question_val[:100],
                "answer": answer_val[:100],
                "rating": fb.rating,
                "tags": fb.tags,
                "comment": fb.comment,
                "status": fb.status,
                "user": fb.user.real_name or fb.user.username if fb.user else "",
                "created_at": fb.created_at.isoformat() if fb.created_at else "",
            })
        return Response({"rows": rows, "count": len(rows)})


class OverviewStatsView(APIView):
    """GET /api/v1/analytics/overview/?root_type= 概览统计
    - Dashboard 顶部卡片：总 QA 次数、近 7 日 QA、准确率、平均延迟、活跃用户、文档数、活跃会话
    - 使用条件聚合一次性查出 total_qa/weekly_qa/good/bad，减少查询
    - 系统级统计，需具备 analytics:system:read 权限
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics:system:read'

    def get(self, request):
        from apps.knowledge.models import Document
        from apps.memory.models import Session

        today = timezone.now().date()
        week_ago = today - timedelta(days=7)
        root_type = request.query_params.get("root_type")

        qa_qs = QaRecord.objects.all()
        if root_type:
            qa_qs = qa_qs.filter(root_type=root_type)

        # 一次性聚合：总 QA + 近 7 日 QA
        qa_agg = qa_qs.aggregate(
            total_qa=models.Count('id'),
            weekly_qa=models.Count('id', filter=models.Q(created_at__date__gte=week_ago)),
        )
        total_qa = qa_agg['total_qa'] or 0
        weekly_qa = qa_agg['weekly_qa'] or 0

        # 反馈聚合：一次性查出好评+差评
        fb_qs = QaFeedback.objects.all()
        if root_type:
            fb_qs = fb_qs.filter(qa_record__root_type=root_type)
        fb_agg = fb_qs.aggregate(
            good_fb=models.Count('id', filter=models.Q(rating__gt=0)),
            bad_fb=models.Count('id', filter=models.Q(rating__lt=0)),
        )
        good_fb = fb_agg['good_fb'] or 0
        bad_fb = fb_agg['bad_fb'] or 0
        accuracy = round(good_fb / max(good_fb + bad_fb, 1), 4)

        # 平均延迟：排除缓存命中（缓存命中延迟仅代表纯读库速度，不是真实 LLM 生成耗时）
        norm_qs = qa_qs.exclude(is_hit_cache=True)
        latency_agg = norm_qs.aggregate(
            avg_total=models.Avg("latency_total_ms"),
            avg_ttfb=models.Avg("latency_ttfb_ms"),
        )
        avg_total_ms = int(latency_agg["avg_total"] or 0)
        avg_ttft_ms = int(latency_agg["avg_ttfb"] or 0)

        active_users = qa_qs.filter(created_at__date__gte=week_ago).values("user").distinct().count()

        # 文档统计：一次性查出 total + completed
        doc_qs = Document.objects.filter(is_deleted=False)
        if root_type:
            doc_qs = doc_qs.filter(root_type=root_type)
        doc_agg = doc_qs.aggregate(
            total_docs=models.Count('id'),
            completed_docs=models.Count('id', filter=models.Q(status='done')),
        )
        total_docs = doc_agg['total_docs'] or 0
        completed_docs = doc_agg['completed_docs'] or 0

        active_sessions = Session.objects.filter(is_deleted=False, last_active_at__date__gte=week_ago).count()

        return Response({
            "total_qa": total_qa,
            "weekly_qa": weekly_qa,
            "accuracy": accuracy,
            "avg_latency_ms": avg_total_ms,   # 兼容旧字段，等于 avg_total_ms
            "avg_ttft_ms": avg_ttft_ms,      # 首字耗时（排除缓存命中）
            "avg_total_ms": avg_total_ms,     # 整体总耗时（排除缓存命中）
            "active_users": active_users,
            "total_docs": total_docs,
            "completed_docs": completed_docs,
            "active_sessions": active_sessions,
        })


class QaRecordView(APIView):
    """GET /api/v1/analytics/qa-records/?start_date=&end_date=&root_type=&qa_id=

    - 返回 QA 记录列表，供 Dashboard 查看对话历史
    - 可选 qa_id 参数：返回单条 QA 详情（用于 QA 详情弹窗，避免列表前 100 条限制）
    - 仅管理员或具备 analytics:system:read 权限的用户可访问
    - 支持日期范围、知识库类型过滤和分页
    - feedback 是 OneToOne，需用 hasattr 判断存在性（select_related 做 LEFT JOIN，无反馈时为 None）
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics:system:read'

    def get(self, request):

        # —— 单条详情：qa_id 参数，用于详情弹窗（绕开分页前 100 条的限制）——
        qa_id = request.query_params.get('qa_id')
        if qa_id:
            try:
                qa_id = int(qa_id)
            except (ValueError, TypeError):
                return Response({'detail': 'qa_id 必须为整数'}, status=400)
            try:
                r = (QaRecord.objects
                     .select_related('feedback')
                     .get(id=qa_id))
            except QaRecord.DoesNotExist:
                return Response({'detail': 'QA 记录不存在'}, status=404)
            # OneToOne LEFT JOIN：无反馈时 r.feedback 为 None
            rating = r.feedback.rating if (hasattr(r, 'feedback') and r.feedback) else 0
            return Response({
                'row': {
                    'id': r.id,
                    'question': r.question,
                    'answer': r.answer,
                    'answer_type': r.answer_type,
                    'root_type': r.root_type,
                    'rating': rating,
                    'latency_total_ms': r.latency_total_ms,
                    'tokens_prompt': r.tokens_prompt,
                    'tokens_completion': r.tokens_completion,
                    'cost_estimate': float(r.cost_estimate),
                    'is_hit_cache': r.is_hit_cache,
                    'created_at': r.created_at.isoformat(),
                }
            })

        # —— 列表查询 ——
        qs = QaRecord.objects.all().select_related("feedback").order_by("-created_at")

        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        root_type = request.query_params.get("root_type")

        # 日期过滤：统一使用 __date__lte / __date__gte 而非 naive datetime，
        # 避免 created_at (timezone-aware) 与 naive datetime 比较触发 RuntimeWarning，
        # 同时保证 end_date 当天的所有记录都能被包含（不会被 end_date 00:00:00 截断）
        if start_date:
            try:
                start = datetime.fromisoformat(start_date).date()
                qs = qs.filter(created_at__date__gte=start)
            except ValueError:
                return Response({'detail': 'start_date 格式应为 YYYY-MM-DD'}, status=400)

        if end_date:
            try:
                end = datetime.fromisoformat(end_date).date()
                qs = qs.filter(created_at__date__lte=end)
            except ValueError:
                return Response({'detail': 'end_date 格式应为 YYYY-MM-DD'}, status=400)

        if root_type:
            qs = qs.filter(root_type=root_type)

        try:
            page = int(request.query_params.get("page") or 1)
            size = int(request.query_params.get("page_size") or 20)
        except (ValueError, TypeError):
            return Response({"detail": "page 和 page_size 必须为整数"}, status=400)

        if page < 1:
            page = 1
        if size < 1 or size > 100:
            size = 20
        offset = (page - 1) * size

        total = qs.count()
        rows = []
        for r in qs[offset:offset + size]:
            # OneToOne LEFT JOIN：无反馈时 r.feedback 为 None
            rating = r.feedback.rating if (hasattr(r, 'feedback') and r.feedback) else 0
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
    """PUT /api/v1/analytics/bad-feedbacks/{id}/ 标记反馈状态

    - 修改反馈状态（pending→resolved/ignored）直接影响用户体验
    - 仅限管理员操作，需记录审计日志
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics:system:write'

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


# ============================================================================
# 以下为系统监控类 API（需要 analytics 权限）
# ============================================================================

class SystemMetricsReportView(APIView):
    """GET /api/v1/analytics/system-metrics/?date=2026-07-24

    - 读取预计算的 SystemMetricsReport，避免实时聚合
    - date 参数可选，默认昨天
    - 仅管理员可访问（系统级敏感指标）
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics:system:read'

    def get(self, request):
        date_str = request.query_params.get('date')
        if date_str:
            try:
                report_date = datetime.fromisoformat(date_str).date()
            except ValueError:
                return Response({'detail': '日期格式应为 YYYY-MM-DD'}, status=400)
        else:
            report_date = (timezone.now() - timedelta(days=1)).date()

        try:
            report = SystemMetricsReport.objects.get(report_date=report_date)
        except SystemMetricsReport.DoesNotExist:
            return Response({
                'date': str(report_date),
                'available': False,
                'message': '该日期的报表尚未生成，请等待凌晨聚合任务完成',
            })

        return Response({
            'date': str(report_date),
            'available': True,
            'total_qa': report.total_qa,
            'cache_hit_count': report.cache_hit_count,
            'normal_qa_count': report.normal_qa_count,
            # 正常请求延迟
            'p50_latency_total': report.p50_latency_total,
            'p95_latency_total': report.p95_latency_total,
            'p99_latency_total': report.p99_latency_total,
            'p50_latency_llm': report.p50_latency_llm,
            'p95_latency_llm': report.p95_latency_llm,
            'p50_latency_retrieval': report.p50_latency_retrieval,
            'p95_latency_retrieval': report.p95_latency_retrieval,
            'p50_ttfb': report.p50_ttfb,
            'p95_ttfb': report.p95_ttfb,
            # 缓存命中延迟
            'cache_hit_p50_latency': report.cache_hit_p50_latency,
            'cache_hit_p95_latency': report.cache_hit_p95_latency,
            # 比率
            'cache_hit_rate': report.cache_hit_rate,
            'llm_success_rate': report.llm_success_rate,
            'llm_timeout_rate': report.llm_timeout_rate,
            'embedding_error_rate': report.embedding_error_rate,
            'avg_tokens_per_second': report.avg_tokens_per_second,
            # Token & 成本
            'total_tokens_prompt': report.total_tokens_prompt,
            'total_tokens_completion': report.total_tokens_completion,
            'total_cost': float(report.total_cost),
            # 分布
            'latency_histogram': report.latency_histogram,
            'error_distribution': report.error_distribution,
        })


class OrgUsageReportView(APIView):
    """GET /api/v1/analytics/org-usage/?date=&department_id=&team_id=

    - 支持按日期、部门、团队筛选
    - department_id/team_id 可选，不传则返回所有
    - 同时返回部门汇总（team_id=-1）和团队明细
    - 权限控制：super_admin 看全部，部门负责人看本部门所有团队，
      团队负责人仅能看本团队（不能看其他团队或部门级汇总）
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics:org:read'

    def get(self, request):
        from apps.users.models import UserTeam

        date_str = request.query_params.get('date')
        department_id = request.query_params.get('department_id')
        team_id = request.query_params.get('team_id')

        # --- 参数校验 ---
        try:
            if date_str:
                report_date = datetime.fromisoformat(date_str).date()
            else:
                report_date = (timezone.now() - timedelta(days=1)).date()
        except ValueError:
            return Response({'detail': '日期格式应为 YYYY-MM-DD'}, status=400)

        try:
            if department_id:
                department_id = int(department_id)
        except (ValueError, TypeError):
            return Response({'detail': 'department_id 必须为整数'}, status=400)

        try:
            if team_id:
                team_id = int(team_id)
        except (ValueError, TypeError):
            return Response({'detail': 'team_id 必须为整数'}, status=400)

        qs = OrgUsageReport.objects.filter(report_date=report_date)

        # --- 权限过滤：按用户角色分级控制可见范围 ---
        user = request.user
        if not user.is_super_admin:
            profile = getattr(user, 'profile', None)
            user_dept_id = profile.department_id if (profile and profile.department_id) else None

            if not user_dept_id:
                # 无部门归属的非 admin 用户无法查看任何组织数据
                return Response({'detail': '无权限访问，请联系管理员'}, status=403)

            # 1) 跨部门访问拦截
            if department_id is not None and department_id != user_dept_id:
                return Response({'detail': '无权查看其他部门数据'}, status=403)

            # 2) 限制在本部门
            qs = qs.filter(department_id=user_dept_id)
            department_id = user_dept_id  # 固定为本部门，后续逻辑复用

            # 3) 团队级限制：若用户仅是某团队负责人（非部门负责人），仅可见本团队
            # 判断是否为团队负责人：遍历 UserTeam 中 role='leader' 的团队
            leading_team_ids = list(UserTeam.objects.filter(
                user_id=user.id,
                role='leader',
                team__department_id=user_dept_id,
            ).values_list('team_id', flat=True))

            # 部门负责人（profile 中 is_dept_head 标记）可见本部门所有团队 + 部门级汇总
            is_dept_head = bool(getattr(profile, 'is_dept_head', False))
            if not is_dept_head and leading_team_ids:
                # 团队负责人：仅允许访问 leading_team_ids 中的团队
                # 禁止访问 team_id=-1（部门级汇总），禁止访问其他团队
                if team_id is not None:
                    if team_id == -1:
                        return Response({'detail': '无权查看部门级汇总'}, status=403)
                    if team_id not in leading_team_ids:
                        return Response({'detail': '无权查看其他团队数据'}, status=403)
                else:
                    # 未指定 team_id，限制在用户负责的团队范围（过滤掉 -1 汇总和其他团队）
                    qs = qs.filter(team_id__in=leading_team_ids)
            elif not is_dept_head:
                # 非部门负责人且非任何团队负责人 → 仅看个人维度不开放组织数据
                return Response({'detail': '无权限访问组织报表，请联系管理员'}, status=403)
            # else: 部门负责人 → 保持在本部门范围，允许查看所有团队 + 部门级汇总

        if department_id is not None:
            qs = qs.filter(department_id=department_id)
        if team_id is not None:
            # 前端传 -1 表示查看部门级汇总（team_id=-1 哨兵值）
            if team_id == -1:
                qs = qs.filter(team_id=-1)
            else:
                qs = qs.filter(team_id=team_id)
        else:
            # team_id 未指定时：默认仅返回团队级明细（team_id != -1），
            # 避免同时返回部门汇总+团队明细造成前端重复统计
            qs = qs.exclude(team_id=-1)

        rows = []
        for r in qs.order_by('department_id', 'team_id'):
            rows.append({
                'id': r.id,
                'report_date': str(r.report_date),
                'department_id': r.department_id,
                'department_name': r.department_name,
                'team_id': r.team_id,
                'team_name': r.team_name,
                'qa_count': r.qa_count,
                'user_count': r.user_count,
                'total_tokens': r.total_tokens,
                'total_cost': float(r.total_cost),
                'avg_latency_ms': r.avg_latency_ms,
                'p95_latency_ms': r.p95_latency_ms,
                'good_feedback_rate': r.good_feedback_rate,
                'cache_hit_count': r.cache_hit_count,
                'cache_hit_rate': r.cache_hit_rate,
            })

        return Response({
            'date': str(report_date),
            'count': len(rows),
            'rows': rows,
        })


class QueueDepthView(APIView):
    """GET /api/v1/analytics/queue-depth/?hours=24

    - 获取最近 N 小时的队列深度历史（PG 数据）
    - 同时返回当前实时深度（Redis）
    - 需具备 analytics:system:read 权限
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics:system:read'

    def get(self, request):
        try:
            hours = int(request.query_params.get('hours', 24))
        except (ValueError, TypeError):
            return Response({'detail': 'hours 必须为整数'}, status=400)

        if hours < 1 or hours > 720:  # 限制 1h ~ 30 天
            return Response({'detail': 'hours 范围: 1-720'}, status=400)

        # --- 历史数据（PG）---
        from apps.analytics.utils import get_queue_depth_history
        history = get_queue_depth_history(hours=hours)

        # --- 当前实时深度（Redis）---
        try:
            from apps.analytics.realtime import get_queue_depth_snapshot
            current = get_queue_depth_snapshot()
        except Exception:
            logger.exception('[QueueDepth] Failed to get current snapshot')
            current = {}

        return Response({
            'hours': hours,
            'current': current,
            'history': history,
        })


class RealtimeSnapshotView(APIView):
    """GET /api/v1/analytics/realtime/

    - 获取今日实时指标快照（Redis 数据）
    - 用于 Dashboard 实时展示，5 分钟更新一次
    - 需具备 analytics:system:read 权限
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics:system:read'

    def get(self, request):
        try:
            from apps.analytics.realtime import get_realtime_snapshot
            snapshot = get_realtime_snapshot()
            return Response(snapshot)
        except Exception:
            logger.exception('[RealtimeSnapshot] Failed to get snapshot')
            return Response({
                'date': timezone.now().date().isoformat(),
                'total_qa': 0,
                'cache_hits': 0,
                'llm_errors': 0,
                'error': 'snapshot_unavailable',
            }, status=200)


class QualityReportView(APIView):
    """GET /api/v1/analytics/quality-reports/?start_date=&end_date=&status=

    - 查询回答忠实度评估报告
    - 支持按日期范围和状态筛选
    - 仅管理员可访问
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics:system:read'

    def get(self, request):
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        status = request.query_params.get('status')

        try:
            page = int(request.query_params.get('page', 1))
            page_size = int(request.query_params.get('page_size', 20))
        except (ValueError, TypeError):
            return Response({'detail': 'page 和 page_size 必须为整数'}, status=400)

        if page < 1:
            page = 1
        if page_size < 1 or page_size > 100:
            page_size = 20

        qs = AnswerQualityReport.objects.select_related('qa_record').order_by('-created_at')

        # 日期过滤：统一使用 __date 后缀，
        # 避免 created_at (aware) 与 naive datetime 比较的 RuntimeWarning，且 end_date 当天全部记录都包含
        if start_date:
            try:
                start = datetime.fromisoformat(start_date).date()
                qs = qs.filter(created_at__date__gte=start)
            except ValueError:
                return Response({'detail': 'start_date 格式应为 YYYY-MM-DD'}, status=400)

        if end_date:
            try:
                end = datetime.fromisoformat(end_date).date()
                qs = qs.filter(created_at__date__lte=end)
            except ValueError:
                return Response({'detail': 'end_date 格式应为 YYYY-MM-DD'}, status=400)

        if status:
            qs = qs.filter(status=status)

        total = qs.count()
        offset = (page - 1) * page_size

        rows = []
        for r in qs[offset:offset + page_size]:
            rows.append({
                'id': r.id,
                'qa_record_id': r.qa_record_id,
                'faithfulness_score': r.faithfulness_score,
                'faithfulness_reason': r.faithfulness_reason,
                'eval_model': r.eval_model,
                'eval_tokens_used': r.eval_tokens_used,
                'eval_cost': float(r.eval_cost),
                'eval_latency_ms': r.eval_latency_ms,
                'status': r.status,
                'error_message': r.error_message,
                'retry_count': r.retry_count,
                'created_at': r.created_at.isoformat(),
            })

        # --- 汇总统计 ---
        summary_agg = qs.aggregate(
            total_evaluated=models.Count('id', filter=models.Q(status='completed')),
            avg_score=models.Avg('faithfulness_score', filter=models.Q(status='completed')),
        )
        total_evaluated = summary_agg['total_evaluated'] or 0
        avg_score = summary_agg['avg_score'] or 0

        return Response({
            'total': total,
            'page': page,
            'page_size': page_size,
            'rows': rows,
            'summary': {
                'total_evaluated': total_evaluated,
                'avg_faithfulness_score': round(float(avg_score), 4),
            },
        })


# ============================================================================
# 黄金测试集管理 Views
# ============================================================================

class GoldenDatasetListView(APIView):
    """GET/POST /api/v1/analytics/golden-datasets/"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics:system:read'

    def get(self, request):
        from apps.analytics.models import GoldenDataset
        status = request.query_params.get('status')
        qs = GoldenDataset.objects.all().order_by('-updated_at')
        if status:
            qs = qs.filter(status=status)
        rows = list(qs[:100].values(
            'id', 'name', 'description', 'root_type', 'status',
            'question_count', 'version', 'created_at', 'updated_at',
        ))
        return Response({'rows': rows, 'count': len(rows)})

    def post(self, request):
        from apps.analytics.offline_eval import create_golden_dataset
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
            'question_count': ds.question_count, 'version': ds.version,
            'questions': questions_data,
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

    def post(self, request, ds_id):
        from apps.analytics.offline_eval import import_questions_from_json
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

    def get(self, request, ds_id):
        from apps.analytics.offline_eval import export_dataset_to_json
        data = export_dataset_to_json(ds_id)
        return Response({'dataset_id': ds_id, 'questions': data})


class GoldenQuestionView(APIView):
    """POST/DELETE /api/v1/analytics/golden-datasets/<ds_id>/questions/"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]

    def post(self, request, ds_id):
        from apps.analytics.offline_eval import import_questions_from_json
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
# 离线评估执行 Views
# ============================================================================

class RunRetrievalEvalView(APIView):
    """POST /api/v1/analytics/eval/retrieval/ - 执行离线检索评估"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]

    def post(self, request):
        from apps.analytics.offline_eval import run_retrieval_evaluation
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

    def post(self, request):
        from apps.analytics.offline_eval import run_answer_quality_evaluation
        dataset_id = request.data.get('dataset_id')
        if not dataset_id:
            return Response({'detail': 'dataset_id 必填'}, status=400)
        try:
            dataset_id = int(dataset_id)
        except (ValueError, TypeError):
            return Response({'detail': 'dataset_id 必须为整数'}, status=400)
        dimensions = request.data.get('dimensions')
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
                dimensions=dimensions,
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
    """GET /api/v1/analytics/eval/retrieval-reports/"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]

    def get(self, request):
        from apps.analytics.models import RetrievalQualityReport
        qs = RetrievalQualityReport.objects.select_related('dataset').order_by('-created_at')
        rows = list(qs[:50].values(
            'id', 'dataset_id', 'eval_batch_id',
            'recall_at_5', 'recall_at_10', 'recall_at_20', 'mrr', 'ndcg_at_5', 'ndcg_at_10',
            'vector_recall_at_10', 'bm25_recall_at_10', 'hybrid_recall_at_10', 'rerank_recall_at_10',
            'total_questions', 'questions_with_hits', 'questions_without_hits',
            'status', 'created_at',
        ))
        return Response({'rows': rows, 'count': len(rows)})


# ============================================================================
# 文档质量 Views
# ============================================================================

class DocumentQualityReportView(APIView):
    """GET /api/v1/analytics/doc-quality/ - 文档质量汇总"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]

    def get(self, request):
        from apps.analytics.doc_quality import get_document_quality_summary
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        root_type = request.query_params.get('root_type')
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
        return Response(get_document_quality_summary(start_date, end_date, root_type))


class RunDocQualityEvalView(APIView):
    """POST /api/v1/analytics/doc-quality/evaluate/ - 触发文档质量评估"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]

    def post(self, request):
        from apps.analytics.doc_quality import evaluate_document_quality, batch_evaluate_document_quality
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
    """GET /api/v1/analytics/doc-quality/reports/"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]

    def get(self, request):
        from apps.analytics.models import DocumentQualityReport
        from apps.analytics.serializers import DocumentQualityReportSerializer
        root_type = request.query_params.get('root_type')
        min_score = request.query_params.get('min_score')

        qs = DocumentQualityReport.objects.select_related('document').order_by('-created_at')
        if root_type:
            qs = qs.filter(document__root_type=root_type)
        if min_score:
            try:
                qs = qs.filter(quality_score__gte=float(min_score))
            except (ValueError, TypeError):
                return Response({'detail': 'min_score 必须为数字'}, status=400)

        total = qs.count()
        # 用 Serializer 替代手动循环构造 dict；document_name/quality_issues 切片等
        # 计算字段已在序列化器中统一实现
        rows = DocumentQualityReportSerializer(qs[:50], many=True).data
        return Response({'total': total, 'rows': rows})


# ============================================================================
# 多维度评估 Views
# ============================================================================

class MultiDimensionScoreView(APIView):
    """GET /api/v1/analytics/multi-dim-scores/ - 多维度评估结果"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]

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
    """POST /api/v1/analytics/multi-dim-eval/ - 触发多维度评估"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]

    def post(self, request):
        from apps.analytics.models import QaRecord
        from apps.analytics.evaluation_engine import evaluate_all_dimensions, build_context_from_qa_record
        qa_id = request.data.get('qa_record_id')
        if not qa_id:
            return Response({'detail': 'qa_record_id 必填'}, status=400)
        dimensions = request.data.get('dimensions')
        try:
            qa = QaRecord.objects.get(id=qa_id)
        except QaRecord.DoesNotExist:
            return Response({'detail': 'QA记录不存在'}, status=404)

        context = build_context_from_qa_record(qa)
        results = evaluate_all_dimensions(
            question=qa.question,
            answer=qa.answer,
            context=context,
            dimensions=dimensions,
            qa_record_id=qa_id,
        )
        return Response({'ok': True, 'results': results})


# ============================================================================
# 覆盖率 & 反馈闭环 Views
# ============================================================================

class CoverageReportView(APIView):
    """GET /api/v1/analytics/coverage/ - 覆盖率报告"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]

    def get(self, request):
        from apps.analytics.coverage import (
            analyze_hot_query_coverage, detect_knowledge_gaps,
            detect_duplicate_chunks, analyze_domain_coverage,
        )
        try:
            days = int(request.query_params.get('days', 7))
        except (ValueError, TypeError):
            return Response({'detail': 'days 必须为整数'}, status=400)
        # 限制 days 范围：1-30 天，防止过大范围扫描全表
        days = max(1, min(days, 30))

        coverage = analyze_hot_query_coverage(days)
        gaps = detect_knowledge_gaps(days)
        duplicates = detect_duplicate_chunks()
        domain = analyze_domain_coverage(days)

        return Response({
            'coverage': coverage,
            'gaps': gaps[:20],
            'gap_count': len(gaps),
            'duplicates': duplicates,
            'domain': domain,
        })


class FeedbackLoopView(APIView):
    """POST /api/v1/analytics/feedback-loop/ - 执行反馈闭环分析"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]

    def post(self, request):
        from apps.analytics.coverage import auto_link_feedback_to_chunks
        try:
            days = int(request.data.get('days', 7))
        except (ValueError, TypeError):
            return Response({'detail': 'days 必须为整数'}, status=400)
        days = max(1, min(days, 30))
        result = auto_link_feedback_to_chunks(days=days)
        return Response(result)


class GenerateCoverageReportView(APIView):
    """POST /api/v1/analytics/coverage/generate/ - 生成覆盖率报告"""
    permission_classes = [IsAuthenticated, CanViewAnalytics]

    def post(self, request):
        from apps.analytics.coverage import generate_coverage_report
        try:
            days = int(request.data.get('days', 7))
        except (ValueError, TypeError):
            return Response({'detail': 'days 必须为整数'}, status=400)
        days = max(1, min(days, 30))
        report = generate_coverage_report(days=days)
        return Response({
            'ok': True,
            'report_id': report.id,
            'report_date': str(report.report_date),
            'coverage_rate': report.hot_query_coverage_rate,
            'gap_count': report.gap_count,
        })


class CoverageReportListView(APIView):
    """GET /api/v1/analytics/coverage/reports/ - 历史覆盖率报告列表

    - 返回最近 50 条报告记录，按日期倒序
    - 供前端「历史报告」面板展示，支持下载和删除
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]

    def get(self, request):
        from apps.analytics.models import CoverageReport
        # 最近 50 条，避免全表返回
        rows = list(
            CoverageReport.objects
            .order_by('-report_date', '-created_at')
            .values(
                'id', 'report_date', 'total_hot_queries', 'covered_queries',
                'uncovered_queries', 'hot_query_coverage_rate', 'gap_count',
                'duplicate_chunk_rate', 'duplicate_chunk_count',
                'feedback_loop_count', 'feedback_resolved_count',
                'created_at',
            )[:50]
        )
        return Response({'rows': rows, 'count': len(rows)})


class CoverageReportDetailView(APIView):
    """DELETE /api/v1/analytics/coverage/reports/<id>/ - 删除覆盖率报告

    - 仅允许删除历史报告，不影响当前覆盖率展示
    - 删除操作记录审计日志
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics:system:write'

    def delete(self, request, report_id):
        from apps.analytics.models import CoverageReport
        try:
            report = CoverageReport.objects.get(id=report_id)
        except CoverageReport.DoesNotExist:
            return Response({'detail': '报告不存在'}, status=404)
        report.delete()
        logger.info(
            'coverage_report_deleted report_id=%s date=%s user=%s',
            report_id, report.report_date, request.user.username
        )
        return Response({'ok': True})


class CoverageReportExportView(APIView):
    """GET /api/v1/analytics/coverage/reports/<id>/export/ - 导出覆盖率报告为 Excel

    - 将单条报告的完整数据导出为 .xlsx 文件
    - 包含：覆盖率概览、知识空白详情、部门/团队覆盖明细
    - 使用 openpyxl 生成多 Sheet Excel
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]

    def get(self, request, report_id):
        from apps.analytics.models import CoverageReport
        try:
            report = CoverageReport.objects.get(id=report_id)
        except CoverageReport.DoesNotExist:
            return Response({'detail': '报告不存在'}, status=404)

        # 使用 openpyxl 生成 Excel，比 csv 支持多 Sheet + 格式化
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment

        wb = Workbook()

        # ===== Sheet 1: 覆盖率概览 =====
        ws1 = wb.active
        ws1.title = '覆盖率概览'
        header_font = Font(bold=True, color='FFFFFF')
        header_fill = PatternFill(start_color='3B82F6', end_color='3B82F6', fill_type='solid')

        overview = [
            ('报告ID', report.id),
            ('报告日期', str(report.report_date)),
            ('生成时间', report.created_at.strftime('%Y-%m-%d %H:%M')),
            ('', ''),
            ('热门查询总数', report.total_hot_queries),
            ('已覆盖查询数', report.covered_queries),
            ('未覆盖查询数', report.uncovered_queries),
            ('热门问题覆盖率', f'{report.hot_query_coverage_rate * 100:.1f}%'),
            ('知识空白数', report.gap_count),
            ('重复切片率', f'{report.duplicate_chunk_rate * 100:.1f}%'),
            ('重复切片数', report.duplicate_chunk_count),
            ('反馈关联数', report.feedback_loop_count),
            ('反馈已解决数', report.feedback_resolved_count),
        ]
        for row_idx, (label, value) in enumerate(overview, 1):
            ws1.cell(row=row_idx, column=1, value=label).font = header_font
            ws1.cell(row=row_idx, column=1).fill = header_fill
            ws1.cell(row=row_idx, column=2, value=value)
        ws1.column_dimensions['A'].width = 20
        ws1.column_dimensions['B'].width = 30

        # ===== Sheet 2: 知识空白详情 =====
        ws2 = wb.create_sheet('知识空白')
        gaps = report.gap_queries or []
        ws2.append(['查询内容', '出现次数', '改进建议'])
        for col in range(1, 4):
            cell = ws2.cell(row=1, column=col)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal='center')
        for g in gaps:
            ws2.append([
                g.get('query', ''),
                g.get('count', 0),
                g.get('suggestion', ''),
            ])
        ws2.column_dimensions['A'].width = 40
        ws2.column_dimensions['B'].width = 12
        ws2.column_dimensions['C'].width = 50

        # ===== Sheet 3: 部门/团队覆盖明细 =====
        ws3 = wb.create_sheet('部门覆盖')
        domain = report.domain_coverage or {}
        domain_list = domain.get('domain_coverage', []) if isinstance(domain, dict) else domain
        ws3.append(['部门/团队', '文档数', '切片数', '占比', '命中率', '下属团队数'])
        for col in range(1, 7):
            cell = ws3.cell(row=1, column=col)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal='center')
        for d in domain_list:
            teams = d.get('teams', []) if isinstance(d, dict) else []
            # 兼容 dict 和 list 两种数据格式
            doc_count = d.get('doc_count', 0) if isinstance(d, dict) else 0
            chunk_count = d.get('chunk_count', 0) if isinstance(d, dict) else 0
            share = d.get('占比', 0) if isinstance(d, dict) else 0
            hit_rate = d.get('query_hit_rate', 0) if isinstance(d, dict) else 0
            name = d.get('name', '') if isinstance(d, dict) else str(d)
            ws3.append([
                name,
                doc_count,
                chunk_count,
                f'{share * 100:.1f}%' if isinstance(share, (int, float)) else str(share),
                f'{hit_rate * 100:.1f}%' if isinstance(hit_rate, (int, float)) else str(hit_rate),
                len(teams),
            ])
            # 如果有子团队，在下一行缩进展示
            if isinstance(teams, list):
                for team_item in teams:
                    if isinstance(team_item, (list, tuple)) and len(team_item) == 2:
                        team_name, team_data = team_item
                        t_docs = team_data.get('doc_count', 0) if isinstance(team_data, dict) else 0
                        t_chunks = team_data.get('chunk_count', 0) if isinstance(team_data, dict) else 0
                        ws3.append([f'  └ {team_name}', t_docs, t_chunks, '', '', ''])
        ws3.column_dimensions['A'].width = 25
        for col in 'BCDEF':
            ws3.column_dimensions[col].width = 12

        # 输出为 HTTP 响应，浏览器直接下载
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)

        filename = f'coverage_report_{report.report_date}.xlsx'
        response = HttpResponse(
            output.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        logger.info(
            'coverage_report_exported report_id=%s user=%s',
            report_id, request.user.username
        )
        return response