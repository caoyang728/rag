"""
Analytics Celery Tasks - 系统指标 & 组织报表 & 队列监控 & RAG 质量评估

定时任务列表：
1. aggregate_daily_report: 已拆分为独立 Beat 任务，此函数仅保留兼容
2. compute_system_metrics_daily: 每日 02:00 聚合前一天系统指标（P50/P95/P99 等）
3. compute_org_usage_daily: 每日 02:10 聚合前一天组织使用数据
4. update_queue_depth_snapshot: 每 5 分钟更新队列深度快照（PG 历史 + Redis 实时）
5. flush_realtime_metrics_task: 每 5 分钟刷新实时指标时间戳
6. batch_evaluate_document_quality: 每日 03:00 批量评估文档质量
7. generate_coverage_report_daily: 每日 03:30 生成知识库覆盖率报告
8. run_multi_dimension_evaluation: 每 2 小时批量回扫未评估对话(混合时间窗+随机)
9. periodic_retrieval_evaluation: 每周一次执行离线检索评估（黄金测试集）

- 使用 @shared_task 装饰器，支持独立 Worker 部署
- 队列命名：analytics（专用队列，避免与业务任务混跑）
- 每个任务都有 try/except + 日志，失败不影响其他任务
- 评估任务有成本控制（.env 可配置），防止高额消费
"""
from datetime import timedelta
from decimal import Decimal
import random

from celery import shared_task
from django.utils import timezone
from loguru import logger

from rag_project.config import AnalyticsConfig

# 显式导入 production_eval 中的 Celery 任务,确保 autodiscover_tasks 能注册
# 原因:Celery 默认只扫描 tasks.py,@shared_task 装饰的任务若定义在其他模块
# (如 production_eval.py),worker 启动时无法发现,导致 .delay() 派发的任务永远 PENDING
from apps.analytics.production_eval import evaluate_sampled_qa  # noqa: F401


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

    # 凌晨 02:00 定时执行：timezone.now().date() 返回 UTC 日期，
    # 与 __date 查询的本地时区转换在凌晨时段相差一天，必须用本地业务日期
    report_date = timezone.localdate() - timedelta(days=1)
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
    - 使用 bulk_create + bulk_update 替代逐条 update_or_create,减少查询次数
    - 唯一标识 (report_date, department_id, team_id)，用 sentinel -1 表示部门级
    """
    from apps.analytics.models import OrgUsageReport
    from apps.analytics.utils import aggregate_org_usage

    # 凌晨定时执行：用本地业务日期而非 UTC 日期，避免 __date 查询错天（同 SystemMetrics 任务）
    report_date = timezone.localdate() - timedelta(days=1)
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

        # --- 批量写入 ---
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
# 5. 每日报表聚合入口（仅保留兼容，实际聚合由独立 Beat 任务执行）
# ============================================================================

@shared_task(name='analytics.aggregate_daily_report', queue='analytics')
def aggregate_daily_report():
    """每日报表聚合入口 — 已拆分为独立定时任务，此函数仅保留兼容

    - 此任务已拆分为 compute_system_metrics_daily / compute_org_usage_daily 两个独立 Beat 任务
    - 保留函数名注册以兼容可能的外部调用，实际为 no-op
    - 如需手动触发聚合，请直接调用上述两个子任务
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
    - SystemMetricsReport：永久保留（每日 1 条，数据量极小）
    - OrgUsageReport：保留 365 天（超出后汇总为年度统计，按需扩展）
    - Realtime Redis keys：TTL 自动过期（REALTIME_RETENTION_DAYS）
    """
    from apps.analytics.models import QueueDepthLog, OrgUsageReport

    now = timezone.now()
    qd_retention = now - timedelta(days=90)
    our_retention = now - timedelta(days=365)

    deleted_qd, _ = QueueDepthLog.objects.filter(
        created_at__lt=qd_retention
    ).delete()

    deleted_our, _ = OrgUsageReport.objects.filter(
        report_date__lt=our_retention.date()
    ).delete()

    logger.info(
        f'[Cleanup] Deleted: QueueDepthLog={deleted_qd}, '
        f'OrgUsageReport={deleted_our}'
    )
    return {
        'ok': True,
        'queue_depth_logs_deleted': deleted_qd,
        'org_usage_reports_deleted': deleted_our,
    }


# ============================================================================
# 8. 文档质量批量评估（每日 03:00）
# ============================================================================

@shared_task(name='analytics.batch_evaluate_document_quality', queue='analytics')
def batch_evaluate_document_quality(days: int = 7):
    """每日批量评估最近 N 天入库文档的解析/切分/向量化质量

    触发时机：文档解析完成后 + 每日批量
    目的：发现解析质量问题（如文本提取不全、切片过碎、向量化失败），
    便于运营及时干预。
    """
    from apps.analytics.doc_quality import batch_evaluate_document_quality as _batch_eval
    try:
        summary = _batch_eval(days=days)
        logger.info(f'[DocQuality] Batch evaluation completed: {summary}')
        return {'ok': True, 'summary': summary}
    except Exception:
        logger.exception('[DocQuality] Batch evaluation failed')
        return {'ok': False, 'error': 'batch_eval_failed'}


# ============================================================================
# 9. 知识库覆盖率报告生成（每日 03:30）
# ============================================================================

@shared_task(name='analytics.generate_coverage_report_daily', queue='analytics')
def generate_coverage_report_daily(days: int = 7):
    """每日生成知识库覆盖率报告

    分析热门问题覆盖率、知识空白、重复切片、领域覆盖情况、反馈闭环。
    是知识库运营的核心参考数据。
    """
    from apps.analytics.coverage import generate_coverage_report
    try:
        report = generate_coverage_report(days=days)
        logger.info(
            f'[Coverage] Daily report: date={report.report_date}, '
            f'coverage={report.hot_query_coverage_rate:.1%}, '
            f'gaps={report.gap_count}'
        )
        return {
            'ok': True,
            'report_id': report.id,
            'coverage_rate': report.hot_query_coverage_rate,
            'gap_count': report.gap_count,
        }
    except Exception:
        logger.exception('[Coverage] Report generation failed')
        return {'ok': False, 'error': 'coverage_report_failed'}


# ============================================================================
# 10. 多维度回答质量批量评估（每 2 小时）
# ============================================================================

@shared_task(name='analytics.run_multi_dimension_evaluation', queue='analytics')
def run_multi_dimension_evaluation(batch_size: int = None):
    """每 2 小时批量执行多维度回答质量评估

    选取未评估的 QA 记录,用 DeepEval 12 维指标评估;
    与 production_eval.evaluate_sampled_qa 互补(保底+采样负责即时路径,批量负责回扫未覆盖项),
    共用同一引擎与 MultiDimensionScore 表(update_or_create 幂等)。

    选取策略(混合时间窗 + 随机):
    1. 优先取最近 2h 窗口内未评估的(最相关,刚发生还没被采到)
    2. 不足 batch_size 时扩展到当天更早时段(保证用满预算,覆盖更广)
    3. 随机选取(Python 层 random.sample,避免 DB ORDER BY RANDOM() 性能问题)

    成本控制:复用 production_eval 的 _check_daily_budget,日限/成本限自动拦截。
    """
    from apps.analytics.models import QaRecord, MultiDimensionScore
    from apps.analytics.deepeval_metrics import evaluate_with_deepeval
    from apps.analytics.production_eval import _build_context_list, _check_daily_budget, _get_redis
    from rag_project.config import AnalyticsConfig

    # 成本检查
    if not AnalyticsConfig.eval_enabled():
        return {'ok': True, 'skipped': True, 'reason': 'disabled'}

    # 日预算检查(批量回扫前一次性检查,避免逐条检查的开销)
    try:
        r = _get_redis()
        passed, reason = _check_daily_budget(r)
        if not passed:
            return {'ok': True, 'skipped': True, 'reason': reason}
    except Exception as e:
        logger.warning(f'[MultiDimEval] 日预算检查异常,继续评估: {e}')

    # batch_size 默认从配置读取(支持 env 覆盖)
    if batch_size is None:
        batch_size = AnalyticsConfig.production_eval_batch_size()

    now = timezone.now()
    two_hours_ago = now - timedelta(hours=2)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # 已评估的 QA ID:最近 24h 内有评估记录的(避免短时间内重复评估)
    since_24h = now - timedelta(hours=24)
    evaluated_qa_ids = set(
        MultiDimensionScore.objects.filter(
            created_at__gte=since_24h
        ).values_list('qa_record_id', flat=True)
    )

    # 窗口1:最近 2h 未评估的 QA ID(最相关,刚发生还没被保底/采样覆盖)
    window1_ids = list(
        QaRecord.objects.filter(
            created_at__gte=two_hours_ago,
            is_success=True,
        ).exclude(id__in=evaluated_qa_ids).exclude(
            answer_type='refused',
        ).values_list('id', flat=True)
    )
    selected_ids = random.sample(window1_ids, min(len(window1_ids), batch_size))

    # 窗口2:当天但 2h 之前未评估的,补足剩余名额(与窗口1时间不重叠)
    remaining = batch_size - len(selected_ids)
    if remaining > 0:
        window2_ids = list(
            QaRecord.objects.filter(
                created_at__gte=today_start,
                created_at__lt=two_hours_ago,
                is_success=True,
            ).exclude(id__in=evaluated_qa_ids).exclude(
                answer_type='refused',
            ).values_list('id', flat=True)
        )
        selected_ids.extend(
            random.sample(window2_ids, min(len(window2_ids), remaining))
        )

    if not selected_ids:
        logger.info('[MultiDimEval] 无待评估 QA 记录')
        return {'ok': True, 'evaluated': 0}

    pending_qa = QaRecord.objects.filter(id__in=selected_ids)
    eval_model = AnalyticsConfig.eval_model()
    count = 0
    for qa in pending_qa:
        try:
            contexts = _build_context_list(qa)
            if not contexts:
                continue

            results = evaluate_with_deepeval(
                question=qa.question,
                answer=qa.answer,
                contexts=contexts,
                model=eval_model,
            )
            eval_batch_id = f'batch_{timezone.now().strftime("%Y%m%d%H%M%S")}_{qa.id}'
            # 显式写入 created_at=now():与 production_eval 保持一致,
            # auto_now_add 在 UPDATE 分支不生效,需手动覆盖使重新评估的时间
            # 反映到 created_at,避免看板时间窗口过滤丢掉重评记录
            now = timezone.now()
            for res in results:
                MultiDimensionScore.objects.update_or_create(
                    qa_record_id=qa.id,
                    dimension=res['dimension'],
                    defaults={
                        'score': res['score'],
                        'reason': res['reason'],
                        'eval_model': f'deepeval-{eval_model}',
                        'eval_latency_ms': res.get('latency_ms', 0),
                        'eval_batch_id': eval_batch_id,
                        'status': 'completed',
                        'created_at': now,
                    },
                )
            count += 1
        except Exception:
            logger.warning(f'[MultiDimEval] Failed for QA {qa.id}')
            continue

    logger.info(
        f'[MultiDimEval] Evaluated {count}/{len(selected_ids)} QA records '
        f'(window1_2h={len(window1_ids)}, window2_today={remaining > 0})'
    )
    return {'ok': True, 'evaluated': count}


# ============================================================================
# 11. 低分对话归因分析(评估落库后异步触发)
# ============================================================================

@shared_task(name='analytics.run_low_score_analysis', queue='analytics')
def run_low_score_analysis(
    qa_id: int,
    threshold: float = None,
    skip_budget_check: bool = False,
):
    """对单条 QA 执行低分归因分析 → 落 LowScoreAnalysis

    触发场景:
    1. evaluate_sampled_qa 评估完成后,若 QA 均分 < threshold 自动派发
    2. 管理员在看板手动触发(跳过预算检查)

    流程:
    1. 取该 QA 的 12 维评分,计算均分
    2. 均分 >= threshold 或无低分维度 → 不归因(避免无意义分析)
    3. 调 analyze_low_score_qa 执行规则归因 + 分层建议生成
    4. update_or_create 落库(同 QA 重新归因覆盖旧记录)

    成本控制:
    - LLM 建议仅对关键低分触发(详见 low_score_analyzer._should_trigger_llm)
    - 手动触发场景 skip_budget_check=True,不阻塞用户主动分析
    - 自动触发场景由 evaluate_sampled_qa 调用方控制(评估已过日预算检查,归因复用同一预算)

    Args:
        qa_id: QaRecord.id
        threshold: 低分阈值;None 用默认 0.5
        skip_budget_check: True 跳过日预算检查(手动触发场景)

    Returns:
        {'ok': bool, 'reason': str, 'category': str}
    """
    from decimal import Decimal
    from apps.analytics.models import QaRecord, MultiDimensionScore, LowScoreAnalysis
    from apps.analytics.low_score_analyzer import (
        analyze_low_score_qa, DEFAULT_THRESHOLD,
    )
    from rag_project.config import AnalyticsConfig

    if not AnalyticsConfig.eval_enabled():
        return {'ok': True, 'skipped': True, 'reason': 'disabled'}

    threshold_val = threshold if threshold is not None else DEFAULT_THRESHOLD

    try:
        qa = QaRecord.objects.get(id=qa_id)
    except QaRecord.DoesNotExist:
        logger.warning(f'[LowScoreAnalysis] QA 不存在 qa_id={qa_id}')
        return {'ok': False, 'reason': 'qa_not_found'}

    # 取 12 维评分(一次查询,后续归因复用)
    scores = list(
        MultiDimensionScore.objects
        .filter(qa_record_id=qa_id)
        .values('dimension', 'score', 'reason')
    )
    if not scores:
        return {'ok': False, 'reason': 'no_scores'}

    avg_score = sum(float(s.get('score') or 0) for s in scores) / len(scores)
    # 均分达标则不归因(避免无意义分析 + 节省成本)
    if avg_score >= threshold_val:
        return {'ok': True, 'skipped': True, 'reason': 'score_above_threshold',
                'avg_score': round(avg_score, 4)}

    try:
        result = analyze_low_score_qa(
            qa_record_id=qa_id,
            scores=scores,
            threshold=threshold_val,
        )
        # update_or_create:同 QA 重新归因覆盖旧记录,保留最新结论
        LowScoreAnalysis.objects.update_or_create(
            qa_record_id=qa_id,
            defaults={
                'avg_score': result['avg_score'],
                'threshold': threshold_val,
                'root_cause_category': result['category'],
                'root_cause_detail': result['detail'],
                'affected_layer': result['affected_layer'],
                'low_dimensions': result['low_dimensions'],
                'diagnosis': result['diagnosis'],
                'suggestions': result['suggestions'],
                'analysis_method': result['method'],
                'analysis_model': result['model'],
                'analysis_tokens_used': result['tokens'],
                'analysis_cost': Decimal(str(result['cost'])),
                'analysis_latency_ms': result['latency_ms'],
                'status': 'completed',
                'error_message': '',
            },
        )
        logger.info(
            f'[LowScoreAnalysis] qa_id={qa_id} category={result["category"]} '
            f'method={result["method"]} avg={result["avg_score"]}'
        )
        return {
            'ok': True, 'category': result['category'],
            'method': result['method'], 'avg_score': result['avg_score'],
        }
    except Exception as e:
        logger.exception(f'[LowScoreAnalysis] 归因失败 qa_id={qa_id}: {e}')
        # 失败也落一条 failed 记录,前端能看到失败状态
        LowScoreAnalysis.objects.update_or_create(
            qa_record_id=qa_id,
            defaults={
                'avg_score': avg_score,
                'threshold': threshold_val,
                'status': 'failed',
                'error_message': str(e)[:500],
            },
        )
        return {'ok': False, 'reason': f'analysis_failed: {e}'}


# ============================================================================
# 12. 周期性离线检索评估（每周）
# ============================================================================

@shared_task(name='analytics.periodic_retrieval_evaluation', queue='analytics')
def periodic_retrieval_evaluation():
    """每周对所有活跃黄金测试集执行离线检索评估

    用于回归测试：检索参数变更后自动评估质量变化。
    如果测试集为空则跳过。
    """
    from apps.analytics.models import GoldenDataset
    from apps.analytics.offline_eval import run_retrieval_evaluation
    from apps.users.models import User, GrantStatus

    datasets = GoldenDataset.objects.filter(
        status='active',
        question_count__gt=0,
    )
    if not datasets.exists():
        logger.info('[PeriodicEval] No active golden datasets, skipping')
        return {'ok': True, 'skipped': True}

    # 使用系统用户执行评估
    sys_user = User.objects.filter(username='system').first()
    if not sys_user:
        # 项目 User 模型无 is_superuser 字段，超管通过 super_admin 内置角色关联判定
        sys_user = User.objects.filter(
            user_role_rels__role__role_key='super_admin',
            user_role_rels__status=GrantStatus.ACTIVE,
        ).first()
    if not sys_user:
        return {'ok': False, 'error': 'no_user_for_eval'}

    results = []
    for ds in datasets:
        try:
            report = run_retrieval_evaluation(dataset_id=ds.id, user=sys_user)
            results.append({
                'dataset': ds.name,
                'recall_at_10': report.recall_at_10,
                'mrr': report.mrr,
            })
        except Exception:
            logger.warning(f'[PeriodicEval] Failed for dataset {ds.id}')
            continue

    logger.info(f'[PeriodicEval] Evaluated {len(results)} datasets: {results}')
    return {'ok': True, 'evaluated_datasets': len(results), 'results': results}


# ============================================================================
# 13. 低分回归测试集 - 沉淀(每日) + 全链路评估(每周)
# ============================================================================

@shared_task(name='analytics.siphon_low_score_regression', queue='analytics')
def siphon_low_score_regression():
    """每日从生产低分对话沉淀到回归测试集

    选取最近已评估的 QA 中均分最低的 top N,按 root_type 分流到对应的
    regression_low_score 测试集,超出容量上限时按 pass_count 降序淘汰。
    关闭开关(LOW_SCORE_REGRESSION_ENABLED=0)时跳过,手动触发不受影响。
    """
    if not AnalyticsConfig.low_score_regression_enabled():
        logger.debug('[RegressionSiphon] 低分回归已关闭,跳过定时沉淀')
        return {'ok': True, 'skipped': True, 'reason': 'disabled'}

    from apps.analytics.regression_eval import siphon_low_score_qa_to_regression_set
    try:
        result = siphon_low_score_qa_to_regression_set()
        logger.info(f'[RegressionSiphon] done: {result}')
        return {'ok': True, **result}
    except Exception:
        logger.exception('[RegressionSiphon] 沉淀失败')
        return {'ok': False, 'error': 'siphon_failed'}


@shared_task(name='analytics.run_regression_evaluation', queue='analytics')
def run_regression_evaluation_task(dataset_id: int = None, limit: int = None):
    """每周对低分回归测试集执行全链路评估,更新 pass_count

    全链路:检索→生成→12 维评估,均分 ≥ threshold 视为通过(pass_count +1),
    否则重置为 0。成本较高(每问题 90~180s + LLM 费用),故每周一次。

    Args:
        dataset_id: 指定测试集;None 评估所有 regression_low_score 测试集
        limit: 每个测试集最多评估的问题数(手动触发时可限制成本)
    """
    if not AnalyticsConfig.low_score_regression_enabled():
        logger.debug('[RegressionEval] 低分回归已关闭,跳过定时评估')
        return {'ok': True, 'skipped': True, 'reason': 'disabled'}

    from apps.analytics.regression_eval import run_regression_evaluation
    from apps.users.models import User

    # 评估需要一个用户做权限过滤,复用 periodic_retrieval_evaluation 的取用户逻辑
    sys_user = User.objects.filter(username='system').first()
    if not sys_user:
        sys_user = User.objects.filter(is_superuser=True).first()
    if not sys_user:
        return {'ok': False, 'error': 'no_user_for_eval'}

    try:
        result = run_regression_evaluation(dataset_id=dataset_id, user=sys_user, limit=limit)
        logger.info(
            f'[RegressionEval] done: evaluated={result["evaluated"]} '
            f'passed={result["passed"]} failed={result["failed"]}'
        )
        return {'ok': True, **result}
    except Exception:
        logger.exception('[RegressionEval] 评估失败')
        return {'ok': False, 'error': 'eval_failed'}