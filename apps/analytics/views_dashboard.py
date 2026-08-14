"""
analytics views - 运营看板（日报/趋势/QA 记录/差评反馈/系统监控）
"""
from loguru import logger
from datetime import timedelta, datetime

from django.db import models
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.models import SystemMetricsReport, OrgUsageReport
from apps.chat.models import QaRecord, QaFeedback
from apps.users.permissions import CanViewAnalytics

class DailyReportView(APIView):
    """GET /api/v1/analytics/daily/ 日报
    - 展示最近 2 天（今日/昨日）的 QA 概览 + 反馈统计
    - 使用条件聚合一次性查出 qa_count/good/bad，避免 3 次 COUNT 查询
    - 仅聚合实时数据（T+1 精确指标请使用 SystemMetricsReport）
    - 支持 root_type 参数（领域过滤），与前端 loadDailyReport 传参对齐
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request):
        from django.db.models.functions import TruncDate

        # 本地业务日期而非 UTC 日期：timezone.now().date() 在凌晨时段与 PG
        # __date/TruncDate 的本地时区转换相差一天，导致今日数据统计错天（与 Trend 视图一致）
        today = timezone.localdate()
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
    required_perm = 'analytics.system.read'

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
            end_date = timezone.localdate()
            start_date = end_date - timedelta(days=days - 1)

        # 构造 QaRecord 基础过滤 QS，先不要调用 values()，方便后续复用两次聚合
        qa_base_qs = QaRecord.objects.filter(
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
        )
        if root_type:
            qa_base_qs = qa_base_qs.filter(root_type=root_type)

        # 聚合 1：qa count + 缓存命中数（全量；缓存命中单独计数，
        # 供概览趋势图勾选展示"缓存命中"指标）
        qa_counts = (qa_base_qs
                      .annotate(day=TruncDate("created_at"))
                      .values("day")
                      .annotate(qa_count=models.Count("id"),
                                cache_hits=models.Count("id", filter=models.Q(is_hit_cache=True)))
                      .order_by("day"))
        qa_map = {r["day"].isoformat(): {"qa_count": r["qa_count"], "cache_hit_count": r["cache_hits"]} for r in qa_counts}

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

        # 聚合 2b：每日活跃用户数（按 user 去重），供概览趋势图勾选"活跃用户"指标
        active_by_day = (qa_base_qs
                         .annotate(day=TruncDate("created_at"))
                         .values("day")
                         .annotate(active_users=models.Count("user", distinct=True))
                         .order_by("day"))
        active_map = {r["day"].isoformat(): r["active_users"] for r in active_by_day}

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
                "cache_hit_count": qa_row.get("cache_hit_count", 0),
                "active_users": active_map.get(day_str, 0),
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
    required_perm = 'analytics.system.read'

    def get(self, request):
        from apps.analytics.serializers import BadFeedbackSerializer

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
        rows = BadFeedbackSerializer(qs.order_by("-created_at")[:top], many=True).data
        return Response({"rows": rows, "count": len(rows)})


class QaRecordView(APIView):
    """GET /api/v1/analytics/qa-records/?start_date=&end_date=&root_type=&qa_id=
            &q=&answer_type=&cache=&rating=&latency_min=&latency_max=

    - 返回 QA 记录列表，供 Dashboard 查看对话历史
    - 可选 qa_id 参数：返回单条 QA 详情（用于 QA 详情弹窗，避免列表前 100 条限制）
    - 仅管理员或具备 analytics:system:read 权限的用户可访问
    - 支持日期范围、问题搜索、回答类型、缓存命中、评分、延迟区间过滤和分页
    - feedback 是 OneToOne，需用 hasattr 判断存在性（select_related 做 LEFT JOIN，无反馈时为 None）
    """
    permission_classes = [IsAuthenticated, CanViewAnalytics]
    required_perm = 'analytics.system.read'

    def get(self, request):
        from apps.analytics.serializers import QaRecordSerializer

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
            # OneToOne LEFT JOIN：无反馈时 r.feedback 为 None，序列化器内判空
            return Response({'row': QaRecordSerializer(r).data})

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

        # —— 列表筛选：问题搜索 / 回答类型 / 是否缓存 / 评分 / 延迟区间 ——
        q = request.query_params.get("q", "").strip()
        if q:
            # 问题关键词模糊搜索
            qs = qs.filter(question__icontains=q)

        answer_type = request.query_params.get("answer_type")
        if answer_type:
            qs = qs.filter(answer_type=answer_type)

        cache = request.query_params.get("cache")
        if cache in ("true", "1"):
            qs = qs.filter(is_hit_cache=True)
        elif cache in ("false", "0"):
            qs = qs.filter(is_hit_cache=False)

        rating = request.query_params.get("rating")
        if rating in ("-1", "0", "1"):
            if rating == "0":
                # 评分 0：无反馈记录（feedback 为 NULL）与中性反馈都视为"未评分/中性"
                qs = qs.filter(models.Q(feedback__isnull=True) | models.Q(feedback__rating=0))
            else:
                qs = qs.filter(feedback__rating=int(rating))

        latency_min = request.query_params.get("latency_min")
        latency_max = request.query_params.get("latency_max")
        try:
            if latency_min:
                qs = qs.filter(latency_total_ms__gte=int(latency_min))
            if latency_max:
                qs = qs.filter(latency_total_ms__lte=int(latency_max))
        except ValueError:
            return Response({'detail': 'latency_min/latency_max 必须为整数'}, status=400)

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
        rows = QaRecordSerializer(qs[offset:offset + size], many=True).data

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
    required_perm = 'analytics.system.write'

    def put(self, request, fb_id):
        status = request.data.get("status", "resolved")
        if status not in ("pending", "processing", "resolved", "ignored"):
            return Response({"detail": "无效的状态值"}, status=400)

        try:
            fb = QaFeedback.objects.get(id=fb_id)
            fb.status = status
            fb.save(update_fields=["status"])
            logger.info(
                f"feedback_status_updated fb_id={fb.id} status={status} user={request.user.username}"
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
    required_perm = 'analytics.system.read'

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
            'p99_latency_llm': report.p99_latency_llm,
            'p50_latency_retrieval': report.p50_latency_retrieval,
            'p95_latency_retrieval': report.p95_latency_retrieval,
            'p99_latency_retrieval': report.p99_latency_retrieval,
            'p50_ttfb': report.p50_ttfb,
            'p95_ttfb': report.p95_ttfb,
            'p99_ttfb': report.p99_ttfb,
            # 缓存命中延迟
            'cache_hit_p50_latency': report.cache_hit_p50_latency,
            'cache_hit_p95_latency': report.cache_hit_p95_latency,
            'cache_hit_p99_latency': report.cache_hit_p99_latency,
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
    required_perm = 'analytics.org.read'

    def get(self, request):
        from apps.analytics.serializers import OrgUsageSerializer

        # 权限模型：user.department FK 直接获取部门，团队管辖通过 UserTeamScopeRel 判定，
        # 部门管辖通过 UserDeptScopeRel 判定
        from apps.users.models import (
            UserDeptScopeRel, UserTeamScopeRel, GrantStatus,
        )

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
        # super_admin 看全部；部门管理者看本部门所有团队 + 部门级汇总；
        # 团队负责人仅看本团队（不能看其他团队或部门级汇总）
        user = request.user
        if not user.is_super_admin:
            # User 直接有 department FK（单主部门）
            user_dept_id = user.department_id

            if not user_dept_id:
                # 无部门归属的非 admin 用户无法查看任何组织数据
                return Response({'detail': '无权限访问，请联系管理员'}, status=403)

            # 1) 跨部门访问拦截
            if department_id is not None and department_id != user_dept_id:
                return Response({'detail': '无权查看其他部门数据'}, status=403)

            # 2) 限制在本部门
            qs = qs.filter(department_id=user_dept_id)
            department_id = user_dept_id  # 固定为本部门，后续逻辑复用

            # 3) 判断部门管理者：有 UserDeptScopeRel 活跃授权 → 可见本部门所有团队 + 部门级汇总
            #    基于 RBAC 授权关系判定，非硬编码标记
            is_dept_manager = UserDeptScopeRel.objects.filter(
                user=user, dept_id=user_dept_id, status=GrantStatus.ACTIVE,
            ).exists()

            # 4) 团队级限制：通过 UserTeamScopeRel 获取用户管辖的团队（限本部门内）
            #    user.team 为单团队 FK，团队管辖授权通过 UserTeamScopeRel 显式绑定
            leading_team_ids = list(UserTeamScopeRel.objects.filter(
                user=user, status=GrantStatus.ACTIVE,
                team__department_id=user_dept_id,
            ).values_list('team_id', flat=True))

            if not is_dept_manager and leading_team_ids:
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
            elif not is_dept_manager:
                # 非部门管理者且非任何团队负责人 → 仅看个人维度不开放组织数据
                return Response({'detail': '无权限访问组织报表，请联系管理员'}, status=403)
            # else: 部门管理者 → 保持在本部门范围，允许查看所有团队 + 部门级汇总

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

        rows = OrgUsageSerializer(qs.order_by('department_id', 'team_id'), many=True).data

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
    required_perm = 'analytics.system.read'

    def get(self, request):
        try:
            hours = int(request.query_params.get('hours', 24))
        except (ValueError, TypeError):
            return Response({'detail': 'hours 必须为整数'}, status=400)

        if hours < 1 or hours > 720:  # 限制 1h ~ 30 天
            return Response({'detail': 'hours 范围: 1-720'}, status=400)

        # --- 历史数据（PG）---
        from apps.analytics.services.aggregation_service import get_queue_depth_history
        history = get_queue_depth_history(hours=hours)

        # --- 当前实时深度（Redis）---
        try:
            from apps.analytics.services.realtime_service import get_queue_depth_snapshot
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
    required_perm = 'analytics.system.read'

    def get(self, request):
        try:
            from apps.analytics.services.realtime_service import get_realtime_snapshot, get_yesterday_same_period_stats
            snapshot = get_realtime_snapshot()
            # 同比对比数据：昨日同时段累计（失败时为 None，前端按"无对比数据"降级展示）
            snapshot['yesterday'] = get_yesterday_same_period_stats()
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

