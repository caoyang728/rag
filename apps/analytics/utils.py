"""
analytics utils - 统计辅助函数

功能：
- 百分位数计算（P50/P95/P99）
- 延迟直方图构建
- 系统指标日报聚合（QaRecord → SystemMetricsReport）
- 组织使用报表聚合（QaRecord → OrgUsageReport）
- 忠实度评估 Prompt 构建
- 队列深度历史查询辅助

说明：
- 所有聚合函数均为纯 Python，不依赖 Django ORM 聚合，
  原因：P50/P95/P99 需要全量排序，ORM 无法直接实现
- 聚合函数接收 queryset，调用方负责 filter 日期范围
- 每个函数都有 docstring 说明输入/输出/用途
"""
import json
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from django.db import models
from django.utils import timezone
from loguru import logger


# ============================================================================
# 1. 百分位数计算
# ============================================================================

def calculate_percentile(values: list, percentile: float) -> int:
    """计算百分位数（线性插值法）

    说明：
    - 使用标准统计公式：index = p * (n - 1)，线性插值
    - 空列表返回 0，单元素返回该值
    - 返回 int，便于直接存入 IntegerField

    Args:
        values: 数值列表（延迟毫秒数等）
        percentile: 百分位 0-100，如 50 表示 P50

    Returns:
        百分位对应的整数值
    """
    if not values:
        return 0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 1:
        return int(sorted_vals[0])
    # 线性插值公式
    k = (percentile / 100.0) * (n - 1)
    f = int(k)
    c = k - f
    if f + 1 < n:
        return int(sorted_vals[f] + c * (sorted_vals[f + 1] - sorted_vals[f]))
    return int(sorted_vals[f])


def calculate_percentiles(values: list) -> dict:
    """一次性计算 P50/P95/P99

    Args:
        values: 数值列表

    Returns:
        {'p50': int, 'p95': int, 'p99': int}
    """
    return {
        'p50': calculate_percentile(values, 50),
        'p95': calculate_percentile(values, 95),
        'p99': calculate_percentile(values, 99),
    }


# ============================================================================
# 2. 延迟直方图
# ============================================================================

def build_latency_histogram(values: list, bucket_size: int = 100) -> dict:
    """构建延迟直方图（按 bucket_size 毫秒分桶）

    说明：
    - 分桶示例：0-100, 100-200, 200-300, ...
    - 最后一桶为 "max+" 表示超过最大桶的所有值
    - 用于 Dashboard 展示延迟分布

    Args:
        values: 延迟毫秒列表
        bucket_size: 桶宽（毫秒，默认 100）

    Returns:
        {'0-100': 123, '100-200': 456, ...}
    """
    if not values:
        return {}

    histogram = {}
    max_bucket_start = 0

    for v in values:
        bucket_start = (int(v) // bucket_size) * bucket_size
        bucket_end = bucket_start + bucket_size
        bucket_label = f'{bucket_start}-{bucket_end}'
        histogram[bucket_label] = histogram.get(bucket_label, 0) + 1
        max_bucket_start = max(max_bucket_start, bucket_start)

    # 按桶范围排序返回
    sorted_keys = sorted(histogram.keys(), key=lambda x: int(x.split('-')[0]))
    return {k: histogram[k] for k in sorted_keys}


# ============================================================================
# 3. 系统指标日报聚合（QaRecord → SystemMetricsReport）
# ============================================================================

def aggregate_system_metrics(report_date: Optional[date] = None) -> dict:
    """聚合指定日期的系统指标（供 SystemMetricsReport 写入）

    说明：
    - 区分缓存命中 / 正常请求的延迟指标，防止缓存命中稀释 P50/P95/P99
    - error_distribution 基于 error_type 字段的计数聚合
    - avg_tokens_per_second 仅统计非缓存请求
    - 所有统计基于 QaRecord 已落库的数据，保证一致性

    Args:
        report_date: 统计日期，默认昨天

    Returns:
        dict，字段与 SystemMetricsReport 对应（不含 report_date）
    """
    from apps.chat.models import QaRecord

    if report_date is None:
        report_date = (timezone.now() - timedelta(days=1)).date()

    qs = QaRecord.objects.filter(created_at__date=report_date)

    # --- 总量统计（单次聚合，避免 3 次 count 查询）---
    # 用条件聚合一次查出 total/cache_hit/normal 三个计数，
    agg_counts = qs.aggregate(
        total_qa=models.Count('id'),
        cache_hit_count=models.Count('id', filter=models.Q(is_hit_cache=True)),
        normal_qa_count=models.Count('id', filter=models.Q(is_hit_cache=False)),
    )
    total_qa = agg_counts['total_qa'] or 0
    cache_hit_count = agg_counts['cache_hit_count'] or 0
    normal_qa_count = agg_counts['normal_qa_count'] or 0

    # --- 复用 queryset 做后续查询 ---
    cache_hit_qs = qs.filter(is_hit_cache=True)
    normal_qa_qs = qs.filter(is_hit_cache=False)

    # --- 正常请求延迟 ---
    # .values_list 返回的结果中，未设置的字段会是 None；
    # calculate_percentile 使用 sorted(values)，混入 None 会触发 TypeError，
    # 所以必须用 comprehension 过滤掉 None 再计算百分位
    normal_total_latencies = [
        v for v in normal_qa_qs.values_list('latency_total_ms', flat=True)
        if v is not None
    ]
    normal_llm_latencies = [
        v for v in normal_qa_qs.values_list('latency_llm_ms', flat=True)
        if v is not None
    ]
    normal_retrieval_latencies = [
        v for v in normal_qa_qs.values_list('latency_retrieval_ms', flat=True)
        if v is not None
    ]
    normal_ttfb_latencies = [
        v for v in normal_qa_qs.values_list('latency_ttfb_ms', flat=True)
        if v is not None
    ]

    total_percentiles = calculate_percentiles(normal_total_latencies)
    llm_percentiles = calculate_percentiles(normal_llm_latencies)
    retrieval_percentiles = calculate_percentiles(normal_retrieval_latencies)
    ttfb_percentiles = calculate_percentiles(normal_ttfb_latencies)

    # --- 缓存命中延迟 ---
    # 与正常请求相同：过滤掉 None，防止 calculate_percentile sorted() 崩溃
    cache_hit_latencies = [
        v for v in cache_hit_qs.values_list('latency_total_ms', flat=True)
        if v is not None
    ]
    cache_hit_percentiles = calculate_percentiles(cache_hit_latencies)

    # --- 比率指标---
    rate_agg = normal_qa_qs.aggregate(
        success_count=models.Count('id', filter=models.Q(is_success=True)),
        timeout_count=models.Count('id', filter=models.Q(error_type='timeout')),
    )
    success_count = rate_agg['success_count'] or 0
    timeout_count = rate_agg['timeout_count'] or 0

    embedding_error_count = qs.aggregate(
        cnt=models.Count('id', filter=models.Q(error_type='embedding_error'))
    )['cnt'] or 0

    # --- 比率计算（基于条件聚合结果）---
    cache_hit_rate = cache_hit_count / max(total_qa, 1)
    llm_success_rate = success_count / max(normal_qa_count, 1)
    llm_timeout_rate = timeout_count / max(normal_qa_count, 1)
    embedding_error_rate = embedding_error_count / max(total_qa, 1)

    # Token 生成速率（仅非缓存请求）
    # 过滤 None：未统计 tokens_per_second 的记录不参与平均，防止 sum(None) TypeError 或拉高 len()
    tokens_per_sec_values = [
        v for v in normal_qa_qs.values_list('tokens_per_second', flat=True)
        if v is not None
    ]
    avg_tokens_per_second = (
        sum(tokens_per_sec_values) / len(tokens_per_sec_values)
        if tokens_per_sec_values else 0.0
    )

    # --- Token & 成本（仅非缓存请求）---
    token_agg = normal_qa_qs.aggregate(
        total_prompt=models.Sum('tokens_prompt'),
        total_completion=models.Sum('tokens_completion'),
        total_cost=models.Sum('cost_estimate'),
    )
    total_tokens_prompt = int(token_agg['total_prompt'] or 0)
    total_tokens_completion = int(token_agg['total_completion'] or 0)
    total_cost = float(token_agg['total_cost'] or 0)

    # --- 错误分布 ---
    error_dist = {}
    error_rows = (qs.exclude(error_type='')
                   .values_list('error_type', flat=True))
    for err_type in error_rows:
        error_dist[err_type] = error_dist.get(err_type, 0) + 1

    # --- 延迟直方图（仅非缓存请求）---
    latency_histogram = build_latency_histogram(normal_total_latencies)

    return {
        'total_qa': total_qa,
        'cache_hit_count': cache_hit_count,
        'normal_qa_count': normal_qa_count,
        # 正常请求延迟百分位
        'p50_latency_total': total_percentiles['p50'],
        'p95_latency_total': total_percentiles['p95'],
        'p99_latency_total': total_percentiles['p99'],
        'p50_latency_llm': llm_percentiles['p50'],
        'p95_latency_llm': llm_percentiles['p95'],
        'p50_latency_retrieval': retrieval_percentiles['p50'],
        'p95_latency_retrieval': retrieval_percentiles['p95'],
        'p50_ttfb': ttfb_percentiles['p50'],
        'p95_ttfb': ttfb_percentiles['p95'],
        # 缓存命中延迟
        'cache_hit_p50_latency': cache_hit_percentiles['p50'],
        'cache_hit_p95_latency': cache_hit_percentiles['p95'],
        # 比率指标
        'cache_hit_rate': round(cache_hit_rate, 4),
        'llm_success_rate': round(llm_success_rate, 4),
        'llm_timeout_rate': round(llm_timeout_rate, 4),
        'embedding_error_rate': round(embedding_error_rate, 4),
        'avg_tokens_per_second': round(avg_tokens_per_second, 2),
        # Token & 成本
        'total_tokens_prompt': total_tokens_prompt,
        'total_tokens_completion': total_tokens_completion,
        'total_cost': Decimal(f"{total_cost:.6f}"),
        # 直方图 & 错误分布
        'latency_histogram': latency_histogram,
        'error_distribution': error_dist,
    }


# ============================================================================
# 4. 组织使用报表聚合（QaRecord → OrgUsageReport）
# ============================================================================

def aggregate_org_usage(report_date: Optional[date] = None) -> list:
    """聚合指定日期的组织使用数据（部门 + 团队双粒度）

    说明：
    - 返回 list，每个元素是一个 OrgUsageReport 的 dict 数据
    - 同时生成部门级汇总（team_id=None）和团队明细
    - 好评率计算：QaFeedback 中 rating>0 / (rating>0 + rating<0)

    Args:
        report_date: 统计日期，默认昨天

    Returns:
        list[dict]，每个 dict 对应一个 (department, team) 组合
    """
    from apps.chat.models import QaRecord, QaFeedback
    from apps.users.models import User

    if report_date is None:
        report_date = (timezone.now() - timedelta(days=1)).date()

    qs = QaRecord.objects.filter(created_at__date=report_date)

    # 构建 user → org 映射
    # User 直接有 department FK 和 team FK
    user_ids = list(qs.values_list('user_id', flat=True).distinct())

    # --- User 部门 + 团队信息（单团队，从 User 直接获取，一次查询）---
    user_dept_map = {}  # user_id → {department_id, department_name}
    user_teams_map = {}  # user_id → [{team_id, team_name, team_dept_id, team_dept_name}]
    for u in User.objects.filter(id__in=user_ids).select_related('department', 'team__department'):
        if u.department_id:
            user_dept_map[u.id] = {
                'department_id': u.department_id,
                'department_name': u.department.name if u.department else '',
            }
        # 单团队归属：user.team 是 FK，None 表示无团队
        # 保留 list 结构以兼容下游多团队遍历逻辑（每用户最多 1 个团队）
        if u.team_id:
            team_dept_id = u.team.department_id if u.team else None
            team_dept_name = u.team.department.name if u.team and u.team.department else ''
            user_teams_map[u.id] = [{
                'team_id': u.team_id,
                'team_name': u.team.name if u.team else '',
                'team_dept_id': team_dept_id,
                'team_dept_name': team_dept_name,
            }]

    # --- 按 (dept, team) 聚合 ---
    org_data = {}  # key: (dept_id, team_id), value: agg dict

    for qa in qs.select_related('user').iterator():
        user_id = qa.user_id
        dept_info = user_dept_map.get(user_id)

        # 匿名用户或无部门归属的跳过组织聚合
        if dept_info is None:
            continue

        dept_id = dept_info['department_id']
        dept_name = dept_info['department_name']

        # --- 部门级汇总（team_id=-1 哨兵值，避免 NULL != NULL 唯一约束问题）---
        dept_key = (dept_id, -1)
        if dept_key not in org_data:
            org_data[dept_key] = {
                'department_id': dept_id,
                'department_name': dept_name,
                'team_id': -1,
                'team_name': '',
                'qa_count': 0,
                'user_ids': set(),
                'total_tokens': 0,
                'total_cost': Decimal('0'),
                'latencies': [],
                'cache_hit_count': 0,
            }
        od = org_data[dept_key]
        od['qa_count'] += 1
        od['user_ids'].add(user_id)
        # tokens_prompt / tokens_completion / cost_estimate / latency_total_ms 均可能为 None，
        # 未回填时按 0 处理（或 latency 跳过入列表），避免 TypeError
        od['total_tokens'] += (qa.tokens_prompt or 0) + (qa.tokens_completion or 0)
        od['total_cost'] += qa.cost_estimate or Decimal('0')
        if qa.latency_total_ms is not None:
            od['latencies'].append(qa.latency_total_ms)
        if qa.is_hit_cache:
            od['cache_hit_count'] += 1

        # --- 团队级明细（遍历用户所属的每个团队）---
        user_teams = user_teams_map.get(user_id, [])
        for team_info in user_teams:
            team_dept_id = team_info.get('team_dept_id')
            # 团队可能属于不同部门（虚拟跨部门团队），以团队自身部门为准
            actual_dept_id = team_dept_id or dept_id
            actual_dept_name = team_info.get('team_dept_name') or dept_name
            team_key = (actual_dept_id, team_info['team_id'])
            if team_key not in org_data:
                org_data[team_key] = {
                    'department_id': actual_dept_id,
                    'department_name': actual_dept_name,
                    'team_id': team_info['team_id'],
                    'team_name': team_info['team_name'],
                    'qa_count': 0,
                    'user_ids': set(),
                    'total_tokens': 0,
                    'total_cost': Decimal('0'),
                    'latencies': [],
                    'cache_hit_count': 0,
                }
            td = org_data[team_key]
            td['qa_count'] += 1
            td['user_ids'].add(user_id)
            # 同样处理 None：tokens/cost 默认 0，latency 仅非 None 入列
            td['total_tokens'] += (qa.tokens_prompt or 0) + (qa.tokens_completion or 0)
            td['total_cost'] += qa.cost_estimate or Decimal('0')
            if qa.latency_total_ms is not None:
                td['latencies'].append(qa.latency_total_ms)
            if qa.is_hit_cache:
                td['cache_hit_count'] += 1

    # --- 计算好评率（QA 记录级，避免多团队用户重复计数）---
    # 遍历 QaFeedback → 通过 QaRecord.user_id 定位所属 (dept, team) → 仅给对应团队 +1
    feedback_qs = QaFeedback.objects.filter(
        qa_record__created_at__date=report_date
    ).select_related('qa_record__user')

    # fb_by_org_key: {(dept_id, team_id): {'good': 0, 'bad': 0}}
    fb_by_org_key = {}
    for fb in feedback_qs:
        if not (fb.qa_record and fb.qa_record.user_id):
            continue
        # 用户团队归属：从 user_teams_map 中查找（与聚合 QA 时同一套映射逻辑）
        user_id = fb.qa_record.user_id
        dept_info = user_dept_map.get(user_id)
        if not dept_info:
            continue
        # 1) 部门级汇总（team_id=-1）加 1
        dept_key = (dept_info['department_id'], -1)
        if dept_key not in fb_by_org_key:
            fb_by_org_key[dept_key] = {'good': 0, 'bad': 0}
        if fb.rating > 0:
            fb_by_org_key[dept_key]['good'] += 1
        elif fb.rating < 0:
            fb_by_org_key[dept_key]['bad'] += 1
        # 2) 团队级明细加 1（该反馈对应的 QA 记录用户的每个所属团队各 +1，
        #    与 QA 聚合逻辑保持一致，避免用户多团队时统计口径不一致）
        user_teams = user_teams_map.get(user_id, [])
        for team_info in user_teams:
            actual_dept_id = team_info.get('team_dept_id') or dept_info['department_id']
            t_key = (actual_dept_id, team_info['team_id'])
            if t_key not in fb_by_org_key:
                fb_by_org_key[t_key] = {'good': 0, 'bad': 0}
            if fb.rating > 0:
                fb_by_org_key[t_key]['good'] += 1
            elif fb.rating < 0:
                fb_by_org_key[t_key]['bad'] += 1

    # --- 组装结果 ---
    results = []
    for key, od in org_data.items():
        dept_id, team_id = key

        # 好评率：从 QA 记录级聚合结果中读取，避免多团队用户的重复计数
        fb = fb_by_org_key.get(key, {'good': 0, 'bad': 0})
        org_good = fb['good']
        org_bad = fb['bad']
        good_feedback_rate = (
            org_good / (org_good + org_bad)
            if (org_good + org_bad) > 0 else 0.0
        )

        avg_latency = (
            sum(od['latencies']) / len(od['latencies'])
            if od['latencies'] else 0
        )
        p95_latency = calculate_percentile(od['latencies'], 95)
        # 防御性：qa_count 至少为 1（因为 key 仅在 for qa in qs 循环中初始化）
        # 但加上 max(1) 防止空字典导致的除零
        cache_hit_rate = (
            od['cache_hit_count'] / max(od['qa_count'], 1)
        )

        results.append({
            'report_date': report_date,
            'department_id': dept_id,
            'department_name': od['department_name'],
            'team_id': team_id,
            'team_name': od['team_name'],
            'qa_count': od['qa_count'],
            'user_count': len(od['user_ids']),
            'total_tokens': od['total_tokens'],
            'total_cost': od['total_cost'],
            'avg_latency_ms': int(avg_latency),
            'p95_latency_ms': p95_latency,
            'good_feedback_rate': round(good_feedback_rate, 4),
            'cache_hit_count': od['cache_hit_count'],
            'cache_hit_rate': round(cache_hit_rate, 4),
        })

    return results


# ============================================================================
# 5. 忠实度评估 Prompt 构建
# ============================================================================

FAITHFULNESS_PROMPT_TEMPLATE = """你是一名回答忠实度评估专家。请评估以下回答是否忠实于原文内容。

## 问题
{question}

## 原文内容（检索到的知识片段）
{context}

## 回答
{answer}

## 评估要求
请从以下维度评估回答的忠实度（0-1 分）：
1. 回答中的事实是否都能在原文中找到依据
2. 回答是否遗漏了原文中的关键信息
3. 回答是否添加了原文中没有的内容（幻觉）
4. 回答是否正确理解了原文的含义

请以 JSON 格式输出：
{{
    "score": 0.xx,
    "reason": "评估理由（中文，不超过100字）"
}}"""


def build_faithfulness_prompt(question: str, context: str, answer: str) -> str:
    """构建忠实度评估 Prompt

    - 使用固定模板，保证评估结果可复现
    - context 为拼接的检索片段，过长时截断
    - answer 为最终回答，直接传入

    Args:
        question: 用户问题
        context: 检索到的知识片段（拼接文本）
        answer: 生成的回答

    Returns:
        完整的 Prompt 字符串
    """
    # context 截断到 4000 字符，防止超出模型上下文窗口
    if len(context) > 4000:
        context = context[:4000] + '...[截断]'

    return FAITHFULNESS_PROMPT_TEMPLATE.format(
        question=question[:500],
        context=context,
        answer=answer[:2000],
    )


def parse_faithfulness_result(llm_output: str) -> tuple:
    """解析忠实度评估的 LLM 输出

    - 尝试解析 JSON 格式的输出
    - 解析失败时返回默认值 (0.0, '评估解析失败')
    - 容忍 LLM 输出中可能的 ```json``` 包装

    Args:
        llm_output: LLM 返回的原始文本

    Returns:
        (score: float, reason: str)
    """
    # 去除可能的 markdown 代码块包装
    cleaned = re.sub(r'```json\s*|\s*```', '', llm_output.strip())

    try:
        result = json.loads(cleaned)
        score = float(result.get('score', 0.0))
        reason = str(result.get('reason', ''))[:200]
        # 分数钳位到 [0.0, 1.0]
        score = max(0.0, min(1.0, score))
        return score, reason
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"[Faithfulness] Failed to parse LLM output: {e}, raw: {llm_output[:200]}")
        return 0.0, '评估结果解析失败'


# ============================================================================
# 6. 队列深度历史查询辅助
# ============================================================================

def get_queue_depth_history(hours: int = 24) -> list:
    """获取队列深度历史数据（供 Dashboard 趋势图使用）

    - 从 PG QueueDepthLog 查询最近 N 小时的数据
    - 返回扁平数组，前端可直接按 minute_bucket 分组聚合
    - 使用 created_at 索引，查询效率高

    Args:
        hours: 查询最近多少小时（默认 24）

    Returns:
        [{queue_name, minute_bucket, queued_size, active_size, worker_count, failed_count}, ...]
        每个元素是某条队列在某个时间槽的快照；minute_bucket 为 202607302130（YYYYMMDDHHmm）格式
        空数据时返回 [] 而非 {}，避免前端 Array.map 报错
    """
    from apps.analytics.models import QueueDepthLog

    since = timezone.now() - timedelta(hours=hours)
    logs = QueueDepthLog.objects.filter(
        created_at__gte=since
    ).order_by('created_at', 'queue_name')

    result = []
    for log in logs:
        # 兼容老数据：如果有 minute_bucket 优先使用，否则从 created_at 构造
        if hasattr(log, 'minute_bucket') and log.minute_bucket:
            bucket = str(log.minute_bucket)
        else:
            local_time = timezone.localtime(log.created_at)
            bucket = local_time.strftime('%Y%m%d%H%M')

        result.append({
            'queue_name': log.queue_name,
            'minute_bucket': bucket,
            # 新字段 queued_size/active_size，若无则从 depth 和 worker_count 近似
            'queued_size': getattr(log, 'queued_size', log.depth),
            'active_size': getattr(log, 'active_size', 0),
            'worker_count': log.worker_count,
            'failed_count': getattr(log, 'failed_count', 0),
            'depth': log.depth,
        })

    return result