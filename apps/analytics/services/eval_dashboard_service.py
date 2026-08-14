"""analytics service - 评估看板 / 路由分析看板聚合

把 4 个重聚合视图（EvalDashboardOverview / EvalDashboardTrend /
EvalDashboardLowScore / RouteAnalysisDashboard）的聚合逻辑下沉到 service：
- 视图层只做参数解析（输入 serializer）与 Response 包装
- 聚合逻辑集中在 service，便于单独测试与复用
"""
from collections import defaultdict
from datetime import timedelta

from django.db import models
from django.utils import timezone

from apps.analytics.models import MultiDimensionScore, RouteAnalysis
from apps.analytics.views_common import _apply_org_filter_on_qa
from apps.chat.models import QaRecord
from rag_project.config import AnalyticsConfig

# 维度分组定义(与 deepeval_metrics.py 保持一致)
_DIMENSION_GROUPS = {
    'retrieval': ['context_relevancy'],
    'quality': ['faithfulness', 'hallucination', 'answer_relevancy',
                'completeness', 'conciseness', 'clarity'],
    'safety': ['toxicity', 'bias'],
    'business': ['professionalism', 'helpfulness', 'actionability'],
}

# 路由层级固定顺序：前端按此渲染堆叠图/柱状图，缺失的层补 0 而非跳过
ROUTE_ORDER = ['wiki', 'graphrag_local', 'graphrag_global', 'rag']
ROUTE_LABELS = {
    'wiki': 'Wiki 直答',
    'graphrag_local': 'GraphRAG 局部',
    'graphrag_global': 'GraphRAG 全局',
    'rag': 'RAG 兜底',
}


def _aggregate_route_trace_stats(qs):
    """从窗口内 QaRecord.route_trace 单次扫描聚合改写 + 个性化两类统计

    改写与个性化链路都不沉淀到 RouteAnalysis 表(主链 RAG 的 route_source 为空
    会被排除),都从 route_trace 实时统计;原先两个聚合函数各遍历一遍同一范围
    数据,合并为一次扫描,避免大时间窗口下重复全表扫描。

    Returns:
        (query_transform_stats, personalization_stats) 两个 dict
    """
    rows = (qs.exclude(route_trace__isnull=True)
              .exclude(route_trace=[])
              .only('id', 'route_trace')
              .iterator())
    rewrite_total = rewrite_changed = decompose_total = 0
    p_total = adjusted = hit = cold_start = 0
    for qa in rows:
        for entry in (qa.route_trace or []):
            layer = entry.get('layer')
            if layer == 'query_rewrite':
                rewrite_total += 1
                if entry.get('changed'):
                    rewrite_changed += 1
            elif layer == 'query_decompose':
                decompose_total += 1
            elif layer == 'personalization':
                if entry.get('applied'):
                    p_total += 1
                    if entry.get('reordered'):
                        adjusted += 1
                    if entry.get('personalized_hits', 0) > 0:
                        hit += 1
                else:
                    cold_start += 1
    query_transform_stats = {
        'rewrite_total': rewrite_total,
        'rewrite_changed': rewrite_changed,
        # 改写命中率 = 实际改写的次数 / 走了改写链路的问答数,无改写链路时为 0
        'rewrite_hit_rate': round(rewrite_changed / rewrite_total, 4) if rewrite_total else 0.0,
        'decompose_total': decompose_total,
    }
    # 个性化链路统计:开关关闭(无 trace 数据)时各项为 0/0.0,
    # 运营对比开关前后的看板数值即可得到"开启 vs 关闭"的效果差异
    personalization_stats = {
        'personalized_total': p_total,
        'cold_start_count': cold_start,
        'adjusted_count': adjusted,
        'adjust_rate': round(adjusted / p_total, 4) if p_total else 0.0,
        'hit_count': hit,
        'personalized_hit_rate': round(hit / p_total, 4) if p_total else 0.0,
    }
    return query_transform_stats, personalization_stats


def get_eval_dashboard_overview(days, root_type='', dept_id=None, team_id=None, threshold=0.5):
    """评估看板顶部 KPI 聚合（EvalDashboardOverviewView）

    返回 overview 接口的完整响应体（不含 HTTP 包装）：
    - total_evaluated/total_qa/coverage_rate/low_score_rate/safety_alert_count
    - dimension_groups: 4 大类均分 + 各维度均分/环比/7 日趋势
    - 无评估数据时返回空结构（total_evaluated=0 + display_dimensions 白名单），
      避免前端渲染 0 值雷达图/维度标签造成"写死数据"的误解

    Args:
        days: 时间窗口天数（调用方已钳位 1-90）
        root_type: 领域过滤，空串表示全部
        dept_id/team_id: 组织筛选（按提问用户归属），None 表示不筛选
        threshold: 低分阈值，默认 0.5
    """
    since = timezone.now() - timedelta(days=days)

    # 基础 queryset(按时间 + 可选 root_type + 组织筛选)
    scores_qs = MultiDimensionScore.objects.filter(created_at__gte=since)
    qa_qs = QaRecord.objects.filter(created_at__gte=since, is_success=True).exclude(answer_type='refused')
    if root_type:
        scores_qs = scores_qs.filter(qa_record__root_type=root_type)
        qa_qs = qa_qs.filter(root_type=root_type)
    # 组织筛选(按 QaRecord.user 归属 JOIN):
    # scores_qs JOIN 通过 qa_record__ 关联 QaRecord,qa_qs 直接是 QaRecord
    scores_qs = _apply_org_filter_on_qa(scores_qs, dept_id, team_id, qa_prefix='qa_record__')
    qa_qs = _apply_org_filter_on_qa(qa_qs, dept_id, team_id)

    # 1. 评估量(去重 qa_record_id)
    total_evaluated = scores_qs.values('qa_record_id').distinct().count()
    total_qa = qa_qs.count()

    if total_evaluated == 0:
        return {
            'days': days,
            'root_type': root_type or 'all',
            'total_evaluated': 0,
            'total_qa': total_qa,
            'coverage_rate': 0,
            'low_score_count': 0,
            'low_score_rate': 0,
            'safety_alert_count': 0,
            'threshold': threshold,
            'date_range': [],
            'dimension_groups': {},
            # 即使无评估数据也返回展示维度白名单,前端据此渲染维度组的空态结构
            'display_dimensions': AnalyticsConfig.eval_display_dimensions(),
        }

    # 2. 各维度均分(一次 GROUP BY 拿全)
    dim_agg = scores_qs.values('dimension').annotate(
        avg_score=models.Avg('score'),
        count=models.Count('id'),
    )
    dim_map = {r['dimension']: {'avg': float(r['avg_score'] or 0), 'count': r['count']} for r in dim_agg}

    # 3. 每个 QA 的均分(用于低分统计)
    # 用 GROUP BY qa_record_id + Avg,一次查询拿到所有 QA 的均分
    qa_avg = scores_qs.values('qa_record_id').annotate(
        avg_score=models.Avg('score'),
    )
    qa_avg_list = list(qa_avg)
    low_score_count = sum(1 for r in qa_avg_list if float(r['avg_score'] or 0) < threshold)

    # 4. 安全告警:toxicity 或 bias 维度 < 0.5 的 QA 数(精确查 toxicity/bias 维度)
    safety_qs = scores_qs.filter(
        dimension__in=['toxicity', 'bias'], score__lt=0.5
    ).values_list('qa_record_id', flat=True).distinct()
    safety_alert_count = len(set(safety_qs))

    # 5. 每个维度最近 7 天每日均分(用于前端 sparkline + 环比)
    # 一次 TruncDate+dimension 聚合,拿到所有 (date, dimension, avg) 三元组
    # 用本地业务日期取"今天",与项目日界约定一致(timezone.now().date() 是 UTC,
    # 本地 00:00-08:00 期间会取成昨天,导致趋势窗口少/多一天)
    today = timezone.localdate()
    trend_start = today - timedelta(days=max(days, 7) - 1)
    trend_qs = scores_qs.filter(
        created_at__date__gte=trend_start,
    ).annotate(
        _date=models.functions.TruncDate('created_at'),
    ).values('_date', 'dimension').annotate(
        avg_score=models.Avg('score'),
    ).order_by('_date', 'dimension')
    # 按维度分桶: {dim: ['0.8','0.82',...]} 每天一个 float,缺失天用 0 填充
    dim_trend_map = {}
    dim_dates = []
    for r in trend_qs:
        d = r['dimension']
        date_str = r['_date'].isoformat()
        if date_str not in dim_dates:
            dim_dates.append(date_str)
        if d not in dim_trend_map:
            dim_trend_map[d] = {}
        dim_trend_map[d][date_str] = round(float(r['avg_score'] or 0), 4)
    # 补齐所有日期,确保每个维度 trend_7d 长度一致
    dim_dates.sort()
    for d in dim_trend_map:
        dim_trend_map[d] = [dim_trend_map[d].get(dt, 0) for dt in dim_dates]

    # 前一周期均分(环比用):前 7 天(不含当前窗口)的维度均分
    prev_since = since - timedelta(days=days)
    prev_end = since
    prev_scores_qs = MultiDimensionScore.objects.filter(
        created_at__gte=prev_since, created_at__lt=prev_end,
    )
    if root_type:
        prev_scores_qs = prev_scores_qs.filter(qa_record__root_type=root_type)
    # 环比同样按组织过滤,避免口径不一致
    prev_scores_qs = _apply_org_filter_on_qa(prev_scores_qs, dept_id, team_id, qa_prefix='qa_record__')
    prev_dim_agg = prev_scores_qs.values('dimension').annotate(
        prev_avg=models.Avg('score'),
    )
    prev_dim_map = {r['dimension']: round(float(r['prev_avg'] or 0), 4) for r in prev_dim_agg}

    # 6. 组装 4 大类
    groups = {}
    for group_name, dims in _DIMENSION_GROUPS.items():
        dim_list = []
        scores_in_group = []
        for d in dims:
            info = dim_map.get(d)
            if info:
                cur_avg = round(info['avg'], 4)
                prev_avg = prev_dim_map.get(d, 0)
                # 环比变化率(prev 为 0 时无意义,置 None)
                mom_change = round((cur_avg - prev_avg) / prev_avg, 4) if prev_avg > 0 else None
                dim_list.append({
                    'name': d,
                    'avg': cur_avg,
                    'count': info['count'],
                    'prev_avg': prev_avg,
                    'mom_change': mom_change,
                    'trend_7d': dim_trend_map.get(d, []),
                })
                scores_in_group.append(info['avg'])
        groups[group_name] = {
            'avg_score': round(sum(scores_in_group) / len(scores_in_group), 4) if scores_in_group else 0,
            'dimensions': dim_list,
        }

    return {
        'days': days,
        'root_type': root_type or 'all',
        'dept_id': dept_id,
        'team_id': team_id,
        'total_evaluated': total_evaluated,
        'total_qa': total_qa,
        'coverage_rate': round(total_evaluated / total_qa, 4) if total_qa else 0,
        'low_score_count': low_score_count,
        'low_score_rate': round(low_score_count / total_evaluated, 4) if total_evaluated else 0,
        'safety_alert_count': safety_alert_count,
        'threshold': threshold,
        'date_range': dim_dates,
        'dimension_groups': groups,
        # 展示维度白名单：前端据此过滤「回答质量」页的维度画像，
        # 未在白名单中的维度不再展示（由 SystemConfig.EVAL_DISPLAY_DIMENSIONS 控制）
        'display_dimensions': AnalyticsConfig.eval_display_dimensions(),
    }


def get_eval_dashboard_trend(days, root_type='', dimension='', dept_id=None, team_id=None):
    """评估看板趋势线聚合（EvalDashboardTrendView）

    按天聚合各维度均分，返回 {dates, series} 结构：
    - dimension 为空:返回全部维度趋势(前端可切换显示)
    - dimension 指定:只返回该维度趋势
    组织筛选(dept_id/team_id):按提问用户归属过滤。
    """
    since = timezone.now() - timedelta(days=days)
    qs = MultiDimensionScore.objects.filter(created_at__gte=since)
    if root_type:
        qs = qs.filter(qa_record__root_type=root_type)
    qs = _apply_org_filter_on_qa(qs, dept_id, team_id, qa_prefix='qa_record__')
    if dimension:
        qs = qs.filter(dimension=dimension)

    # 按天 + 维度聚合:TruncDate(created_at) + dimension + Avg(score)
    # 一次查询拿到所有 (date, dimension, avg) 三元组
    trend_qs = qs.annotate(
        date=models.functions.TruncDate('created_at'),
    ).values('date', 'dimension').annotate(
        avg_score=models.Avg('score'),
        count=models.Count('id'),
    ).order_by('date', 'dimension')

    # 组装成 {dates: [...], series: [{dimension, scores: [{date, avg, count}]}]}
    # 先 list() 物化一次:直接迭代 trend_qs 两次会重复执行同一条 SQL
    rows = list(trend_qs)
    dates_set = sorted({r['date'].isoformat() for r in rows})
    dim_series = {}
    for r in rows:
        d = r['dimension']
        if d not in dim_series:
            dim_series[d] = []
        dim_series[d].append({
            'date': r['date'].isoformat(),
            'avg': round(float(r['avg_score'] or 0), 4),
            'count': r['count'],
        })

    return {
        'days': days,
        'root_type': root_type or 'all',
        'dept_id': dept_id,
        'team_id': team_id,
        'dimension': dimension or 'all',
        'dates': dates_set,
        'series': [
            {'dimension': d, 'scores': s}
            for d, s in dim_series.items()
        ],
    }


def get_eval_dashboard_low_score(days, root_type='', dept_id=None, team_id=None, limit=20, threshold=0.5):
    """低分对话 Top N 聚合（EvalDashboardLowScoreView）

    按 QA 均分升序返回均分低于 threshold 的对话：
    - 每行含 question/answer 摘要 + 均分 + 最低维度 + 最低分 + root_type + 时间
    - 无低分数据时返回空 rows（带 threshold/dept_id/team_id 供前端展示）
    """
    since = timezone.now() - timedelta(days=days)
    qs = MultiDimensionScore.objects.filter(created_at__gte=since)
    if root_type:
        qs = qs.filter(qa_record__root_type=root_type)
    qs = _apply_org_filter_on_qa(qs, dept_id, team_id, qa_prefix='qa_record__')

    # 每个 QA 的均分 + 最低分维度(子查询取 min)
    qa_agg = qs.values('qa_record_id').annotate(
        avg_score=models.Avg('score'),
        min_score=models.Min('score'),
    ).filter(avg_score__lt=threshold).order_by('avg_score')[:limit]

    if not qa_agg:
        return {'total': 0, 'threshold': threshold, 'rows': [],
                'dept_id': dept_id, 'team_id': team_id}

    # 批量取 QaRecord 详情(避免 N+1)
    qa_ids = [r['qa_record_id'] for r in qa_agg]
    qa_map = {
        q.id: q for q in QaRecord.objects.filter(id__in=qa_ids).only(
            'id', 'question', 'answer', 'root_type', 'created_at', 'user_id'
        )
    }

    # 批量取每个 QA 的最低分维度(一次 GROUP BY)
    min_dim_qs = qs.filter(qa_record_id__in=qa_ids).values(
        'qa_record_id', 'dimension', 'score'
    )
    # 对每个 qa_id 取 score 最低的 dimension
    qa_min_dim = {}
    for r in min_dim_qs:
        qid = r['qa_record_id']
        if qid not in qa_min_dim or r['score'] < qa_min_dim[qid]['score']:
            qa_min_dim[qid] = {'dimension': r['dimension'], 'score': float(r['score'])}

    rows = []
    for r in qa_agg:
        qid = r['qa_record_id']
        q = qa_map.get(qid)
        if not q:
            continue
        min_info = qa_min_dim.get(qid, {'dimension': '-', 'score': 0})
        rows.append({
            'qa_record_id': qid,
            'question': q.question[:80],
            'answer': q.answer[:120],
            'avg_score': round(float(r['avg_score'] or 0), 4),
            'min_dimension': min_info['dimension'],
            'min_score': round(min_info['score'], 4),
            'root_type': q.root_type,
            'created_at': q.created_at.isoformat(),
        })

    return {'total': len(rows), 'threshold': threshold, 'rows': rows,
            'dept_id': dept_id, 'team_id': team_id}


def get_route_analysis(days, dept_id=None, team_id=None):
    """路由分析看板聚合（RouteAnalysisDashboardView）

    四层路由命中率 + 各维均分对比（由 aggregate_route_analysis_daily 任务供数）：
    - coverage_by_route: 每层命中数/占比/平均置信度/平均延迟/平均质量分
    - quality_by_route: 各层 12 维均分对比（按 4 大类分组，柱状/雷达图用）
    - daily_trend: 按天各层命中数（命中趋势堆叠图用）
    - query_transform_stats / personalization_stats: 从 QaRecord.route_trace 实时聚合

    时间窗口按 qa_created_at（提问时间）过滤；组织筛选按提问用户归属子查询
    （qa_record_id 为 BigInteger 非外键，无法直接 JOIN QaRecord，用子查询收敛）。
    """
    since = timezone.now() - timedelta(days=days)

    qs = RouteAnalysis.objects.filter(qa_created_at__gte=since)
    if dept_id or team_id:
        # 组织筛选走 QaRecord.user 归属：子查询把命中 qa 收敛到组织内
        qa_ids_qs = _apply_org_filter_on_qa(QaRecord.objects.all(), dept_id, team_id)
        qs = qs.filter(qa_record_id__in=qa_ids_qs.values('id'))

    total = qs.count()
    if total == 0:
        # 单次扫描同时出改写/个性化统计(无 RouteAnalysis 数据时看板只展示这两块)
        q_transform, p_stats = _aggregate_route_trace_stats(
            _apply_org_filter_on_qa(QaRecord.objects.filter(created_at__gte=since),
                                    dept_id, team_id))
        return {
            'days': days,
            'dept_id': dept_id,
            'team_id': team_id,
            'total': 0,
            'route_order': ROUTE_ORDER,
            'route_labels': ROUTE_LABELS,
            'coverage_by_route': [],
            'quality_by_route': {},
            'daily_trend': [],
            'query_transform_stats': q_transform,
            'personalization_stats': p_stats,
        }

    # 1. 每层命中统计（一次 GROUP BY 拿全）
    route_agg = qs.values('route_source').annotate(
        count=models.Count('id'),
        avg_confidence=models.Avg('confidence'),
        avg_latency=models.Avg('latency_ms'),
        avg_quality=models.Avg('answer_quality'),
    ).order_by('-count')
    coverage_by_route = [
        {
            'route': r['route_source'],
            'count': r['count'],
            'share': round(r['count'] / total, 4),
            'avg_confidence': round(float(r['avg_confidence'] or 0), 4),
            'avg_latency_ms': round(float(r['avg_latency'] or 0), 1),
            'avg_answer_quality': round(float(r['avg_quality'] or 0), 4),
        }
        for r in route_agg
    ]

    # 2. 各层 12 维均分对比
    # 用子查询避免把窗口内 route_qa 全部拉进内存；再取 (qa_record_id -> route) 映射关联
    score_qs = MultiDimensionScore.objects.filter(
        qa_record_id__in=qs.values('qa_record_id'),
    )
    route_qa_map = dict(qs.values_list('qa_record_id', 'route_source'))

    dim_agg = score_qs.values('qa_record_id', 'dimension').annotate(
        avg_score=models.Avg('score'),
    )
    route_dim_avg = defaultdict(dict)  # route -> {dimension: avg}
    for r in dim_agg:
        route = route_qa_map.get(r['qa_record_id'])
        if route:
            route_dim_avg[route][r['dimension']] = round(float(r['avg_score'] or 0), 4)

    quality_by_route = {}
    for route in ROUTE_ORDER:
        dim_map = route_dim_avg.get(route, {})
        if not dim_map:
            quality_by_route[route] = {'overall': None, 'groups': {}, 'dimensions': {}}
            continue
        groups = {}
        for group_name, dims in _DIMENSION_GROUPS.items():
            group_avg = [dim_map[d] for d in dims if d in dim_map]
            if group_avg:
                groups[group_name] = round(sum(group_avg) / len(group_avg), 4)
        # overall = 该层所有已评估维度均分的算术平均（与 4 大类组均一致）
        all_dims = list(dim_map.values())
        quality_by_route[route] = {
            'overall': round(sum(all_dims) / len(all_dims), 4) if all_dims else None,
            'groups': groups,
            'dimensions': dim_map,
        }

    # 3. 按天命中趋势（堆叠图数据）
    daily_qs = qs.annotate(
        _date=models.functions.TruncDate('qa_created_at'),
    ).values('_date', 'route_source').annotate(
        cnt=models.Count('id'),
    ).order_by('_date')
    trend_map = {}
    for r in daily_qs:
        date_str = r['_date'].isoformat()
        trend_map.setdefault(date_str, {})[r['route_source']] = r['cnt']
    daily_trend = []
    for date_str in sorted(trend_map.keys()):
        row = {'date': date_str}
        for route in ROUTE_ORDER:
            row[route] = trend_map[date_str].get(route, 0)
        daily_trend.append(row)

    # 改写/个性化统计:单次扫描 route_trace 同时出两类统计(与无数据分支共用同一函数)
    q_transform, p_stats = _aggregate_route_trace_stats(
        _apply_org_filter_on_qa(QaRecord.objects.filter(created_at__gte=since),
                                dept_id, team_id))
    return {
        'days': days,
        'dept_id': dept_id,
        'team_id': team_id,
        'total': total,
        'route_order': ROUTE_ORDER,
        'route_labels': ROUTE_LABELS,
        'coverage_by_route': coverage_by_route,
        'quality_by_route': quality_by_route,
        'daily_trend': daily_trend,
        'query_transform_stats': q_transform,
        'personalization_stats': p_stats,
    }
