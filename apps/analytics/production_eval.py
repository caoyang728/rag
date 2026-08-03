"""
生产对话自动评估 —— 内联采样 + 令牌桶限速 + 异步评估

对话结束后按采样率 + 令牌桶限速,异步触发 DeepEval 12 维评估(evaluate_with_deepeval),
结果落 MultiDimensionScore。与定时批量任务 run_multi_dimension_evaluation 互补:
采样负责即时代表性样本,批量负责回扫未采样项。

三重成本保护:
1. 采样率(默认 5%):入口拦截
2. 令牌桶(默认 10/min):Redis 原子 INCR
3. 日限(默认 500/日)+ 成本限(默认 1 元/日):Redis 日计数 + DB 成本聚合
"""
import random
import time
from datetime import timedelta
from typing import List, Tuple

from celery import shared_task
from django.db.models import Sum
from django.utils import timezone
from loguru import logger


def _build_context_list(qa_record) -> List[str]:
    """从 QaRecord.retrieval_scores 取检索切片内容,返回 list[str]

    DeepEval 的 FaithfulnessMetric/ContextualRelevancyMetric 需要 retrieval_context
    为 list[str](每片独立),取 Top5 切片,每片截断 500 字控制 token 成本。
    """
    from apps.knowledge.models import DocumentChunk

    chunk_ids = [
        hit.get('chunk_id', '')
        for hit in (qa_record.retrieval_scores or [])[:5]
        if hit.get('chunk_id')
    ]
    if not chunk_ids:
        return []
    chunks = DocumentChunk.objects.filter(id__in=chunk_ids)
    chunk_map = {c.id: c for c in chunks}
    return [
        chunk_map[cid].content[:500]
        for cid in chunk_ids
        if cid in chunk_map and chunk_map[cid].content
    ]


def _get_redis():
    """复用 Analytics 专用 Redis 连接(DB 3),令牌桶与日计数共用"""
    from apps.analytics.realtime import _get_redis_safe
    return _get_redis_safe()


def _acquire_token(rate_per_min: int) -> bool:
    """令牌桶限速:每分钟最多 rate_per_min 个评估

    实现方式:Redis 计数器,key 带分钟时间戳,INCR 后首次 EXPIRE 65s(略大于 60s 容错)。
    INCR 是原子操作,多 worker 并发下也能精确计数。
    超限返回 False,调用方跳过本次评估。

    Redis 故障时保守返回 False:宁可少评估也不打爆 LLM 评估接口。
    """
    try:
        r = _get_redis()
        minute_key = f'analytics:eval_rate:{int(time.time() // 60)}'
        count = r.incr(minute_key)
        if count == 1:
            r.expire(minute_key, 65)
        return count <= rate_per_min
    except Exception as e:
        logger.warning('[ProdEval] 令牌桶 Redis 异常,保守跳过: %s', e)
        return False


def _check_daily_budget(r) -> Tuple[bool, str]:
    """日预算检查:数量上限 + 成本上限

    数量上限用 Redis 日计数器(原子,INCR 后超限回退,不占配额);
    成本上限从 MultiDimensionScore 聚合今日 eval_cost(非原子,但成本是软限制,近似即可)。

    Returns:
        (是否通过, 拒绝原因)
    """
    from rag_project.config import AnalyticsConfig

    daily_limit = AnalyticsConfig.eval_daily_limit()
    cost_limit = AnalyticsConfig.eval_cost_limit()

    # 数量限:Redis 原子计数
    day_key = f'analytics:eval_daily:{timezone.now().strftime("%Y%m%d")}'
    count = r.incr(day_key)
    if count == 1:
        r.expire(day_key, 90000)  # 25h,跨天自动清理
    if count > daily_limit:
        r.decr(day_key)  # 超限回退,不占用配额
        return False, 'daily_limit_exceeded'

    # 成本限:DB 聚合今日已发生成本
    try:
        from apps.analytics.models import MultiDimensionScore
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        total_cost = MultiDimensionScore.objects.filter(
            created_at__gte=today_start,
        ).aggregate(total=Sum('eval_cost'))['total'] or 0
        if float(total_cost) >= cost_limit:
            r.decr(day_key)  # 回退
            return False, 'cost_limit_exceeded'
    except Exception as e:
        logger.warning('[ProdEval] 成本聚合查询失败,仅按数量限: %s', e)

    return True, ''


def maybe_dispatch_eval(qa_record) -> None:
    """生产对话评估入口:在 QaRecord 持久化后调用

    流程:开关 → 过滤无效对话 → 采样率 → 令牌桶 → dispatch Celery 任务
    任一环节不通过都静默跳过(不影响主对话流程)。

    过滤规则:
    - is_success=False:链路中断,无有效回答,不评估
    - answer_type='refused':正常拒答(无相关资料),无评估意义
    - 缓存命中:回答复用历史,评估重复无价值

    该函数在 _persist_qa 同步路径调用,必须轻量(只做判断 + delay),
    实际评估在 Celery 异步执行,不阻塞用户对话响应。
    """
    from rag_project.config import AnalyticsConfig

    try:
        if not AnalyticsConfig.production_eval_enabled():
            return

        # 过滤无效对话
        if not getattr(qa_record, 'is_success', True):
            return
        if getattr(qa_record, 'answer_type', '') == 'refused':
            return
        if getattr(qa_record, 'is_hit_cache', False):
            return

        # 采样率:random < rate 才进入限速环节
        sample_rate = AnalyticsConfig.production_eval_sample_rate()
        if random.random() >= sample_rate:
            return

        # 令牌桶限速
        if not _acquire_token(AnalyticsConfig.production_eval_rate_per_min()):
            logger.debug('[ProdEval] 令牌桶限速,跳过 qa_id=%s', qa_record.id)
            return

        # dispatch 异步评估任务
        evaluate_sampled_qa.delay(qa_record.id)
        logger.info('[ProdEval] 已派发评估任务 qa_id=%s', qa_record.id)
    except Exception as e:
        # 采样钩子异常绝不影响主对话流程
        logger.exception('[ProdEval] 派发评估异常(已忽略): %s', e)


@shared_task(name='analytics.evaluate_sampled_qa', queue='analytics')
def evaluate_sampled_qa(qa_id: int) -> dict:
    """异步评估单条对话:成本检查 → DeepEval 12 维评估 → 落 MultiDimensionScore

    与定时批量任务 run_multi_dimension_evaluation 共用同一套指标与表,便于统一对比。
    实际启用维度由 PRODUCTION_EVAL_METRIC_GROUPS 控制(默认 all=12 维)。

    成本控制:采样时已检查,这里二次检查防止 worker 积压期间超额。

    Args:
        qa_id: QaRecord.id

    Returns:
        {'ok': bool, 'evaluated': int, 'reason': str}
    """
    from decimal import Decimal
    from apps.analytics.models import QaRecord, MultiDimensionScore
    from apps.analytics.deepeval_metrics import evaluate_with_deepeval
    from rag_project.config import AnalyticsConfig

    try:
        qa = QaRecord.objects.get(id=qa_id)
    except QaRecord.DoesNotExist:
        logger.warning('[ProdEval] QA 不存在 qa_id=%s', qa_id)
        return {'ok': False, 'reason': 'qa_not_found'}

    # 日预算二次检查(防止 worker 积压后批量执行时超额)
    try:
        r = _get_redis()
        passed, reason = _check_daily_budget(r)
        if not passed:
            return {'ok': False, 'skipped': True, 'reason': reason}
    except Exception as e:
        logger.warning('[ProdEval] 日预算检查异常,继续评估: %s', e)

    # 构建检索上下文 list(DeepEval retrieval_context 需要 list[str])
    contexts = _build_context_list(qa)
    if not contexts:
        logger.debug('[ProdEval] 无检索上下文,跳过 qa_id=%s', qa_id)
        return {'ok': False, 'reason': 'no_context'}

    eval_batch_id = f'prod_sampled_{timezone.now().strftime("%Y%m%d%H%M%S")}_{qa_id}'
    eval_model = AnalyticsConfig.eval_model()

    try:
        results = evaluate_with_deepeval(
            question=qa.question,
            answer=qa.answer,
            contexts=contexts,
            model=eval_model,
        )
        # 逐维度落库(update_or_create 幂等:同 qa+维度只保留最新一次评估)
        for res in results:
            MultiDimensionScore.objects.update_or_create(
                qa_record_id=qa.id,
                dimension=res['dimension'],
                defaults={
                    'score': res['score'],
                    'reason': res['reason'],
                    'atomic_facts': [],
                    'eval_model': f'deepeval-{eval_model}',
                    'eval_tokens_used': res.get('tokens_used', 0),
                    'eval_cost': Decimal('0'),
                    'eval_latency_ms': res.get('latency_ms', 0),
                    'eval_batch_id': eval_batch_id,
                    'status': 'completed',
                },
            )
        evaluated = sum(1 for r in results if r.get('score', 0) > 0)
        logger.info(
            '[ProdEval] 评估完成 qa_id=%s, 成功维度=%d/%d',
            qa_id, evaluated, len(results),
        )
        return {'ok': True, 'evaluated': evaluated, 'total': len(results)}
    except Exception as e:
        logger.exception('[ProdEval] 评估失败 qa_id=%s: %s', qa_id, e)
        return {'ok': False, 'reason': f'eval_failed: {e}'}
