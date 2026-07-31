"""
Analytics Celery Tasks - 系统指标 & 组织报表 & 队列监控 & 忠实度评估

定时任务列表：
1. aggregate_daily_report: 每日 01:10 聚合准确率日报（已存在，补齐实现）
2. compute_system_metrics_daily: 每日 02:00 聚合前一天系统指标（P50/P95/P99 等）
3. compute_org_usage_daily: 每日 02:10 聚合前一天组织使用数据
4. update_queue_depth_snapshot: 每 5 分钟更新队列深度快照（PG 历史 + Redis 实时）
5. flush_realtime_metrics_task: 每 5 分钟刷新实时指标时间戳
6. run_faithfulness_evaluation: 每小时批量评估回答忠实度

- 使用 @shared_task 装饰器，支持独立 Worker 部署
- 队列命名：analytics（专用队列，避免与业务任务混跑）
- 每个任务都有 try/except + 日志，失败不影响其他任务
- 忠实度评估任务有成本控制（.env 可配置），防止高额消费
"""
from datetime import timedelta
from decimal import Decimal

from celery import shared_task
from django.utils import timezone
from loguru import logger

from rag_project.config import AnalyticsConfig


# ============================================================================
# 1. 每日系统指标聚合
# ============================================================================

@shared_task(name='analytics.compute_system_metrics', queue='analytics')
def compute_system_metrics_daily():
    """聚合前一天的系统指标 → SystemMetricsReport

    - 每日凌晨执行，Dashboard 读取预计算结果，避免实时聚合
    - 使用 update_or_create 实现 UPSERT，重复执行不会产生重复数据
    - 区分缓存命中 / 正常请求的延迟指标
    - 耗时约 30s（10w+ QaRecord 全量排序），建议在凌晨低峰期执行
    """
    from apps.analytics.models import SystemMetricsReport
    from apps.analytics.utils import aggregate_system_metrics

    report_date = (timezone.now() - timedelta(days=1)).date()
    logger.info(f'[SystemMetrics] Start aggregating for {report_date}')

    try:
        data = aggregate_system_metrics(report_date=report_date)
        data['report_date'] = report_date

        report, created = SystemMetricsReport.objects.update_or_create(
            report_date=report_date,
            defaults=data,
        )
        logger.info(
            f'[SystemMetrics] {"Created" if created else "Updated"} '
            f'SystemMetricsReport for {report_date}, total_qa={data["total_qa"]}'
        )
        return {'ok': True, 'report_date': str(report_date), 'created': created}

    except Exception:
        logger.exception(f'[SystemMetrics] Failed to aggregate for {report_date}')
        return {'ok': False, 'error': 'aggregation_failed'}


# ============================================================================
# 2. 每日组织使用数据聚合
# ============================================================================

@shared_task(name='analytics.compute_org_usage', queue='analytics')
def compute_org_usage_daily():
    """聚合前一天的组织使用数据 → OrgUsageReport

    - 同时生成部门级汇总和团队级明细
    - 使用 bulk_create + bulk_update 替代逐条 update_or_create
      原实现每条记录一次 UPDATE/INSERT，50 部门 × 5 团队 = 250 次查询
      优化后仅 3 次查询（1 次读 + 1 次 bulk_update + 1 次 bulk_create）
    - 唯一标识 (report_date, department_id, team_id)，用 sentinel -1 表示部门级
    """
    from apps.analytics.models import OrgUsageReport
    from apps.analytics.utils import aggregate_org_usage

    report_date = (timezone.now() - timedelta(days=1)).date()
    logger.info(f'[OrgUsage] Start aggregating for {report_date}')

    try:
        results = aggregate_org_usage(report_date=report_date)

        if not results:
            logger.info(f'[OrgUsage] No data for {report_date}')
            return {'ok': True, 'date': str(report_date), 'created': 0, 'updated': 0}

        # --- 查找已存在的记录（批量读）---
        # 一次性获取该日期所有已有记录，构建 (dept, team) → model 映射
        existing_map = {}
        for r in OrgUsageReport.objects.filter(report_date=report_date):
            key = (r.department_id, r.team_id)
            existing_map[key] = r

        # --- 分类：更新 vs 新建 ---
        create_list = []
        update_list = []
        update_fields = [
            'qa_count', 'user_count', 'total_tokens', 'total_cost',
            'avg_latency_ms', 'p95_latency_ms', 'good_feedback_rate',
            'cache_hit_count', 'cache_hit_rate',
        ]

        for data in results:
            key = (data['department_id'], data.get('team_id'))
            existing = existing_map.get(key)
            if existing:
                # 已存在 → 更新
                for field in update_fields:
                    if field in data:
                        setattr(existing, field, data[field])
                update_list.append(existing)
            else:
                # 不存在 → 新建
                create_list.append(OrgUsageReport(**data))

        # --- 批量写入（2 次查询 vs 原来的 N 次）---
        created_count = 0
        updated_count = 0
        if update_list:
            OrgUsageReport.objects.bulk_update(update_list, update_fields)
            updated_count = len(update_list)
        if create_list:
            OrgUsageReport.objects.bulk_create(create_list, batch_size=100)
            created_count = len(create_list)

        logger.info(
            f'[OrgUsage] Done for {report_date}: '
            f'created={created_count}, updated={updated_count}'
        )
        return {
            'ok': True, 'report_date': str(report_date),
            'created': created_count, 'updated': updated_count,
        }

    except Exception:
        logger.exception(f'[OrgUsage] Failed to aggregate for {report_date}')
        return {'ok': False, 'error': 'aggregation_failed'}


# ============================================================================
# 3. 队列深度快照（每 5 分钟）
# ============================================================================

@shared_task(name='analytics.update_queue_depth', queue='analytics')
def update_queue_depth_snapshot():
    """每 5 分钟更新 Celery 队列深度快照

    - 通过 Redis LLEN 查询队列长度（O(1) 操作，几乎无开销）
    - 同时写入 Redis 当前值（供实时 API 查询）和 PG 历史表
    - 若 QUEUE_MONITOR_ENABLED=0 则跳过，便于生产故障时临时关闭
    """
    if not AnalyticsConfig.queue_monitor_enabled():
        logger.debug('[QueueDepth] Queue monitor disabled, skipping')
        return {'ok': True, 'skipped': True}

    try:
        from apps.analytics.realtime import update_queue_depth
        update_queue_depth()
        return {'ok': True}
    except Exception:
        logger.exception('[QueueDepth] Failed to update queue depth')
        return {'ok': False, 'error': 'update_failed'}


# ============================================================================
# 4. 实时指标刷新（每 5 分钟）
# ============================================================================

@shared_task(name='analytics.flush_realtime', queue='analytics')
def flush_realtime_metrics_task():
    """每 5 分钟刷新实时指标的同步时间戳

    - 标记 Redis 数据的新鲜度，供 Dashboard 判断是否需要降级
    - 不移动或删除数据，确保 Dashboard 始终可读取
    """
    try:
        from apps.analytics.realtime import flush_realtime_metrics
        flush_realtime_metrics()
        return {'ok': True}
    except Exception:
        logger.exception('[Realtime] Failed to flush metrics')
        return {'ok': False, 'error': 'flush_failed'}


# ============================================================================
# 5. 忠实度评估（每小时）
# ============================================================================

@shared_task(name='analytics.run_faithfulness', queue='analytics')
def run_faithfulness_evaluation():
    """每小时批量评估回答忠实度

    说明：
    - 仅评估 is_hit_cache=False 且 is_success=True 的 QaRecord
    - 使用便宜模型（deepseek-chat），批量大小 .env 可配置
    - 多层成本控制：batch_size（单次量）、daily_limit（日量）、cost_limit（日费）
    - 评估结果写入 AnswerQualityReport，供 Dashboard 展示
    - 使用 Redis 分布式锁防止并发执行导致成本失控

    成本控制逻辑：
    1. 先获取分布式锁，确保同一时间只有一个评估任务在运行
    2. 检查当日已评估数量，若达到 daily_limit 则跳过
    3. 检查当日已消耗费用，若达到 cost_limit 则跳过
    4. 按优先级（时间倒序）取 batch_size 条记录进行评估
    """
    from apps.analytics.models import AnswerQualityReport
    from apps.chat.models import QaRecord
    from apps.analytics.utils import build_faithfulness_prompt, parse_faithfulness_result
    from apps.llm.factory import get_llm

    # --- 分布式锁：防止多个 Worker 并发执行导致成本超限 ---
    lock_key = 'analytics:faithfulness:lock'
    try:
        from apps.analytics.realtime import _get_redis_safe
        r = _get_redis_safe()
        # SET NX + EX 原子获取锁，30 分钟自动过期
        acquired = r.set(lock_key, '1', nx=True, ex=1800)
        if not acquired:
            logger.info('[Faithfulness] Another evaluation is in progress, skipping')
            return {'ok': True, 'skipped': True, 'reason': 'lock_busy'}
    except Exception as e:
        logger.warning(f'[Faithfulness] Failed to acquire lock: {e}, proceeding without lock')
        r = None

    try:
        if not AnalyticsConfig.faithfulness_enabled():
            logger.debug('[Faithfulness] Disabled, skipping')
            return {'ok': True, 'skipped': True}

        batch_size = AnalyticsConfig.faithfulness_batch_size()
        daily_limit = AnalyticsConfig.faithfulness_daily_limit()
        cost_limit = AnalyticsConfig.faithfulness_cost_limit()
        model = AnalyticsConfig.faithfulness_model()

        today = timezone.now().date()

        # --- 检查当日评估量上限 ---
        today_evaluated = AnswerQualityReport.objects.filter(
            created_at__date=today,
            status='completed',
        ).count()
        if today_evaluated >= daily_limit:
            logger.info(f'[Faithfulness] Daily limit reached: {today_evaluated}/{daily_limit}')
            return {'ok': True, 'reason': 'daily_limit_reached', 'evaluated': today_evaluated}

        remaining_quota = daily_limit - today_evaluated

        # --- 检查当日成本上限 ---
        # 因为 F() 表达式不需要全局引入，但此处 aggregate 需要 models.Sum
        from django.db import models as django_models
        today_cost = AnswerQualityReport.objects.filter(
            created_at__date=today,
            status='completed',
        ).aggregate(
            total_cost=django_models.Sum('eval_cost')
        )['total_cost'] or Decimal('0')

        if float(today_cost) >= cost_limit:
            logger.info(f'[Faithfulness] Cost limit reached: {today_cost}/{cost_limit}')
            return {'ok': True, 'reason': 'cost_limit_reached', 'cost': float(today_cost)}

        # --- 计算剩余预算可评估的条数 ---
        # 估算单次评估成本：约 0.002 元 / 500 tokens
        estimated_cost_per_eval = Decimal('0.002')
        remaining_cost_budget = Decimal(str(cost_limit)) - today_cost
        max_by_cost = int(remaining_cost_budget / estimated_cost_per_eval) if estimated_cost_per_eval > 0 else batch_size

        effective_batch = min(batch_size, remaining_quota, max_by_cost)
        if effective_batch <= 0:
            return {'ok': True, 'reason': 'no_quota', 'remaining_quota': remaining_quota}

        # --- 获取待评估记录 ---
        # 使用 ~Exists (反连接) 代替 exclude(id__in=子查询)，
        # PostgreSQL 将其优化为 Hash Anti-Join，比 NOT IN 子查询性能更稳定
        # 尤其是当已评估记录量大（数万条）时，IN 列表会导致 SQL 膨胀
        seven_days_ago = today - timedelta(days=7)

        # 构建子查询：查找已在 AnswerQualityReport 中的 qa_record_id
        from django.db.models import Exists, OuterRef
        evaluated_subquery = AnswerQualityReport.objects.filter(
            qa_record_id=OuterRef('pk'),
            status__in=['completed', 'pending'],
            created_at__date__gte=seven_days_ago,
        )

        candidates = (QaRecord.objects
                      .filter(is_hit_cache=False, is_success=True,
                              created_at__date__gte=seven_days_ago)
                      .annotate(has_report=Exists(evaluated_subquery))
                      .filter(has_report=False)
                      .order_by('-created_at')[:effective_batch])

        if not candidates:
            return {'ok': True, 'reason': 'no_candidates'}

        # --- 初始化 LLM ---
        try:
            # provider 硬编码 deepseek 在此处是安全的，
            # 因为忠实度评估仅需基础模型能力，deepseek-chat 已在 AnalyticsConfig 配置
            llm = get_llm(provider='deepseek', model=model)
        except Exception:
            logger.exception('[Faithfulness] Failed to initialize LLM')
            return {'ok': False, 'error': 'llm_init_failed'}

        evaluated_count = 0
        failed_count = 0

        for qa in candidates:
            try:
                # 构建检索上下文时获取实际 chunk 文本内容
                # 而非仅用 chunk_id，否则 LLM 无法评估回答的忠实度
                context_parts = []
                # 检查 retrieval_scores（含 chunk_id+score 的字典列表），
                # 而非 retrieval_hits（仅 chunk_id 字符串列表），保持数据源一致性
                if qa.retrieval_scores:
                    from apps.knowledge.models import DocumentChunk
                    chunk_ids = [
                        hit.get('chunk_id', '')
                        for hit in (qa.retrieval_scores or [])[:5]
                        if hit.get('chunk_id')
                    ]
                    if chunk_ids:
                        # 批量查询 chunk 内容，避免 N+1
                        chunks = DocumentChunk.objects.filter(id__in=chunk_ids)
                        chunk_map = {c.id: c for c in chunks}
                        for cid in chunk_ids:
                            chunk = chunk_map.get(cid)
                            if chunk and chunk.content:
                                snippet = chunk.content[:300]
                                section = chunk.section_path or ''
                                context_parts.append(
                                    f'[来源: {section}]\n{snippet}'
                                )
                context = '\n\n'.join(context_parts) if context_parts else '（无检索片段）'

                prompt = build_faithfulness_prompt(qa.question, context, qa.answer)

                # 构造 messages 列表，system + user 组合
                # 使用单条 user 消息传递完整 prompt，简化评估逻辑
                messages = [
                    {'role': 'system', 'content': '你是一名严谨的回答忠实度评估专家。'},
                    {'role': 'user', 'content': prompt},
                ]

                # 调用 LLM
                t0 = timezone.now()
                response = llm.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=200,
                    temperature=0.1,
                )
                eval_latency_ms = int((timezone.now() - t0).total_seconds() * 1000)

                llm_output = response.choices[0].message.content
                score, reason = parse_faithfulness_result(llm_output)

                eval_tokens = response.usage.total_tokens if response.usage else 0
                eval_cost = Decimal(str(eval_tokens)) * Decimal('0.000002')  # 约 2 元/1M tokens

                # 使用 update_or_create 而非 objects.create
                # 因为 qa_record 是 OneToOneField，create 会在已存在记录时抛 IntegrityError
                # update_or_create 在首次评估时创建，重试时更新，安全且幂等
                report_data = {
                    'qa_record': qa,
                    'faithfulness_score': score,
                    'faithfulness_reason': reason,
                    'eval_model': model,
                    'eval_tokens_used': eval_tokens,
                    'eval_cost': eval_cost,
                    'eval_latency_ms': eval_latency_ms,
                    'status': 'completed',
                }
                AnswerQualityReport.objects.update_or_create(
                    qa_record=qa,
                    defaults=report_data,
                )
                evaluated_count += 1

            except Exception as e:
                logger.warning(f'[Faithfulness] Failed to evaluate QA#{qa.id}: {e}')
                # 失败记录：先 update_or_create（幂等）→ 再原子 F() 递增 retry_count
                # 关键：defaults 中不动 retry_count（更新时保留原值，新建用 DB default=0），
                # 然后统一做一次 F() +1，保证每次失败都 +1，不会被覆盖成 1 卡死
                try:
                    from django.db.models import F
                    fail_defaults = {
                        'status': 'failed',
                        'error_message': str(e)[:500],
                    }
                    # 新建时 DB default retry_count=0（从模型 default），更新时不动 DB 原值
                    AnswerQualityReport.objects.update_or_create(
                        qa_record=qa,
                        defaults=fail_defaults,
                    )
                    # 原子递增：新建的 0→1，已有 N→N+1
                    AnswerQualityReport.objects.filter(
                        qa_record=qa
                    ).update(retry_count=F('retry_count') + 1)
                except Exception:
                    logger.exception(f'[Faithfulness] Failed to save failure record for QA#{qa.id}')
                failed_count += 1

        logger.info(
            f'[Faithfulness] Done: evaluated={evaluated_count}, failed={failed_count}, '
            f'today_total={today_evaluated + evaluated_count}'
        )
        result = {
            'ok': True,
            'evaluated': evaluated_count,
            'failed': failed_count,
            'today_total': today_evaluated + evaluated_count,
        }
        return result

    except Exception:
        logger.exception('[Faithfulness] Unexpected error during evaluation')
        return {'ok': False, 'error': 'unexpected_error'}
    finally:
        # 释放分布式锁，确保下次调度可以获取
        # 锁有 TTL（30 分钟），即使进程崩溃也会自动过期
        try:
            if r is not None:
                r.delete(lock_key)
        except Exception:
            pass


# ============================================================================
# 6. 旧版每日报表（仅保留向后兼容，不再执行实际聚合）
# ============================================================================

@shared_task(name='analytics.aggregate_daily_report', queue='analytics')
def aggregate_daily_report():
    """旧版每日报表 — 已迁移至独立任务调度，此函数仅打印警告

    - 2026-07 重构：原先 Beat 配置引用此任务名，内部委托两个子任务
      会导致与新的独立 Beat 任务重复执行
    - 改为 no-op，保留任务名注册以兼容可能的外部调用
    - 若需手动触发聚合，请直接调用 compute_system_metrics_daily / compute_org_usage_daily
    """
    logger.warning(
        '[DailyReport] aggregate_daily_report is deprecated and now a no-op. '
        'Use compute_system_metrics_daily / compute_org_usage_daily directly.'
    )
    return {'ok': True, 'skipped': True, 'reason': 'deprecated_noop'}


# ============================================================================
# 7. 数据清理任务（防止历史数据无限增长）
# ============================================================================

@shared_task(name='analytics.cleanup_old_data', queue='analytics')
def cleanup_old_data():
    """每日凌晨清理过期的监控数据

    保留策略：
    - QueueDepthLog：保留 90 天（每 5 分钟 4 条 = 每天 1152 条，90 天约 10 万条）
    - AnswerQualityReport：保留 180 天
    - SystemMetricsReport：永久保留（每日 1 条，数据量极小）
    - OrgUsageReport：保留 365 天（超出后汇总为年度统计，按需扩展）
    - Realtime Redis keys：TTL 自动过期（REALTIME_RETENTION_DAYS）

    仅删除已完成/失败的记录，pending 状态的保留以便重试
    """
    from apps.analytics.models import QueueDepthLog, AnswerQualityReport, OrgUsageReport

    now = timezone.now()
    qd_retention = now - timedelta(days=90)
    aqr_retention = now - timedelta(days=180)
    our_retention = now - timedelta(days=365)

    deleted_qd, _ = QueueDepthLog.objects.filter(
        created_at__lt=qd_retention
    ).delete()

    deleted_aqr, _ = AnswerQualityReport.objects.filter(
        created_at__lt=aqr_retention,
        status__in=['completed', 'failed'],
    ).delete()

    # OrgUsageReport 过期清理：保留最近 365 天
    deleted_our, _ = OrgUsageReport.objects.filter(
        report_date__lt=our_retention.date()
    ).delete()

    logger.info(
        f'[Cleanup] Deleted: QueueDepthLog={deleted_qd}, '
        f'AnswerQualityReport={deleted_aqr}, OrgUsageReport={deleted_our}'
    )
    return {
        'ok': True,
        'queue_depth_logs_deleted': deleted_qd,
        'quality_reports_deleted': deleted_aqr,
        'org_usage_reports_deleted': deleted_our,
    }