"""
生产对话自动评估 —— 保底优先 + 采样兜底 + 令牌桶限速 + 异步评估

对话结束后按"保底评估 + 采样率 + 令牌桶限速"三级策略,异步触发 DeepEval 12 维评估
(evaluate_with_deepeval),结果落 MultiDimensionScore。与定时批量任务
run_multi_dimension_evaluation 互补:即时路径负责保底头部 + 采样尾部,批量负责回扫未覆盖项。

评估入口策略(maybe_dispatch_eval):
1. 保底评估:每小时前 N 条 + 每日前 M 条直接评估(不经采样率),保证低流量初期也有即时信号
2. 采样兜底:保底额度用尽后,剩余对话按采样率(默认 5%)随机抽取
3. 令牌桶限速:Redis 原子 INCR,每分钟最多 rate_per_min 个评估

成本保护:
1. 保底上限(默认 10/h + 50/日):Redis 小时/日计数器,防止保底成本失控
2. 采样率(默认 5%):保底用尽后入口拦截
3. 令牌桶(默认 10/min):Redis 原子 INCR
4. 日限(默认 500/日)+ 成本限(默认 1 元/日):Redis 日计数 + DB 成本聚合
"""
import random
import time
from datetime import timedelta
from typing import List, Optional, Tuple

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
        logger.warning(f'[ProdEval] 令牌桶 Redis 异常,保守跳过: {e}')
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
        logger.warning(f'[ProdEval] 成本聚合查询失败,仅按数量限: {e}')

    return True, ''


def _check_guarantee(r) -> Tuple[bool, str]:
    """保底评估名额检查:每小时前 N 条 + 每日前 M 条直接放行

    采用 INCR 先行再判断的策略(与令牌桶一致),保证多 worker 并发下计数精确:
    1. 先 INCR 小时计数器,若 <= N 则获得小时保底名额
    2. 再 INCR 日计数器,若 <= M 则确认保底成功
    3. 任一超限则回退计数(DECR),返回 False 走采样兜底

    Redis 故障时返回 (False, 'redis_error'):保底不可用则降级为采样,
    宁可少评估也不因 Redis 故障打爆评估接口。

    Returns:
        (是否获得保底名额, 来源标记) 来源: 'guarantee' / 'guarantee_exhausted' / 'redis_error'
    """
    from rag_project.config import AnalyticsConfig

    hourly_limit = AnalyticsConfig.production_eval_hourly_guarantee()
    daily_limit = AnalyticsConfig.production_eval_daily_guarantee()
    now = timezone.now()

    try:
        # 小时计数器:INCR 后判断,超限回退
        hour_key = f'analytics:eval_guarantee_hourly:{now.strftime("%Y%m%d%H")}'
        hour_count = r.incr(hour_key)
        if hour_count == 1:
            r.expire(hour_key, 7200)  # 2h,跨小时自动清理
        if hour_count > hourly_limit:
            r.decr(hour_key)  # 超限回退,不占名额
            return False, 'hourly_exhausted'

        # 日计数器:小时名额已占,再检查日上限
        day_key = f'analytics:eval_guarantee_daily:{now.strftime("%Y%m%d")}'
        day_count = r.incr(day_key)
        if day_count == 1:
            r.expire(day_key, 90000)  # 25h,跨天自动清理
        if day_count > daily_limit:
            r.decr(day_key)  # 日上限超限,回退日计数
            r.decr(hour_key)  # 同时回退小时计数(保底未生效)
            return False, 'daily_exhausted'

        return True, 'guarantee'
    except Exception as e:
        logger.warning(f'[ProdEval] 保底计数 Redis 异常,降级采样: {e}')
        return False, 'redis_error'


def maybe_dispatch_eval(qa_record) -> None:
    """生产对话评估入口:在 QaRecord 持久化后调用

    流程:开关 → 过滤无效对话 → 保底评估/采样兜底 → 令牌桶 → dispatch Celery 任务
    任一环节不通过都静默跳过(不影响主对话流程)。

    过滤规则:
    - is_success=False:链路中断,无有效回答,不评估
    - answer_type='refused':正常拒答(无相关资料),无评估意义
    - 缓存命中:回答复用历史,评估重复无价值

    评估入口策略(保底优先 + 采样兜底):
    - 保底评估:每小时前 N 条 + 每日前 M 条直接评估,保证低流量初期即时质量信号
    - 采样兜底:保底额度用尽后,剩余对话按采样率随机抽取,覆盖长尾

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

        # 保底评估优先:前 N 条/小时 + 前 M 条/天 直接评估(不经采样率)
        # 保底用尽后降级为采样率兜底,覆盖保底之外的随机样本
        # Redis 故障时保底不可用,降级为采样(_check_guarantee 内部已捕获,
        # 但 _get_redis 本身可能抛异常,此处再兜一层)
        is_guaranteed = False
        try:
            r = _get_redis()
            is_guaranteed, _reason = _check_guarantee(r)
        except Exception as e:
            logger.warning(f'[ProdEval] 保底检查异常,降级采样: {e}')

        if not is_guaranteed:
            # 保底名额用尽,走采样率兜底
            sample_rate = AnalyticsConfig.production_eval_sample_rate()
            if random.random() >= sample_rate:
                return

        # 令牌桶限速(保底与采样共享同一令牌桶,统一限速)
        if not _acquire_token(AnalyticsConfig.production_eval_rate_per_min()):
            logger.debug(f'[ProdEval] 令牌桶限速,跳过 qa_id={qa_record.id}')
            return

        # dispatch 异步评估任务
        evaluate_sampled_qa.delay(qa_record.id)
        tag = '保底' if is_guaranteed else '采样'
        logger.info(f'[ProdEval] 已派发评估任务({tag}) qa_id={qa_record.id}')
    except Exception as e:
        # 采样钩子异常绝不影响主对话流程
        logger.exception(f'[ProdEval] 派发评估异常(已忽略): {e}')


@shared_task(name='analytics.evaluate_sampled_qa', queue='analytics')
def evaluate_sampled_qa(
    qa_id: int,
    skip_budget_check: bool = False,
    eval_batch_id: Optional[str] = None,
) -> dict:
    """异步评估单条对话:成本检查 → DeepEval 12 维评估 → 落 MultiDimensionScore

    与定时批量任务 run_multi_dimension_evaluation 共用同一套指标与表,便于统一对比。
    实际启用维度由 PRODUCTION_EVAL_METRIC_GROUPS 控制(默认 all=12 维)。

    成本控制:采样时已检查,这里二次检查防止 worker 积压期间超额。
    手动评估场景(skip_budget_check=True)绕过日预算检查,由调用方自行控制。

    Args:
        qa_id: QaRecord.id
        skip_budget_check: True 时跳过日预算检查(手动评估场景,不计入生产配额)
        eval_batch_id: 指定 eval_batch_id;None 时自动生成(生产采样场景)
            手动评估场景由视图层预生成传入,便于前端通过 batch_id 轮询结果

    Returns:
        {'ok': bool, 'evaluated': int, 'reason': str, 'eval_batch_id': str}
    """
    from decimal import Decimal
    from apps.analytics.models import QaRecord, MultiDimensionScore
    from apps.analytics.deepeval_metrics import evaluate_with_deepeval
    from rag_project.config import AnalyticsConfig

    try:
        qa = QaRecord.objects.get(id=qa_id)
    except QaRecord.DoesNotExist:
        logger.warning(f'[ProdEval] QA 不存在 qa_id={qa_id}')
        return {'ok': False, 'reason': 'qa_not_found'}

    # 日预算二次检查(防止 worker 积压后批量执行时超额)
    # 手动评估场景跳过:用户主动触发,不应被生产配额阻塞
    if not skip_budget_check:
        try:
            r = _get_redis()
            passed, reason = _check_daily_budget(r)
            if not passed:
                return {'ok': False, 'skipped': True, 'reason': reason}
        except Exception as e:
            logger.warning(f'[ProdEval] 日预算检查异常,继续评估: {e}')

    # 构建检索上下文 list(DeepEval retrieval_context 需要 list[str])
    contexts = _build_context_list(qa)
    if not contexts:
        logger.debug(f'[ProdEval] 无检索上下文,跳过 qa_id={qa_id}')
        return {'ok': False, 'reason': 'no_context'}

    # eval_batch_id:手动评估由视图层预生成传入(便于前端轮询),生产采样自动生成
    if not eval_batch_id:
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
        # 显式写入 created_at=now():auto_now_add 只在首次创建时生效,
        # 重新评估走 UPDATE 分支时不会更新 created_at,导致看板时间窗口
        # (filter(created_at__gte=since)) 过滤掉重新评估的旧记录。
        # 这里手动覆盖,使 created_at 反映"最新评估时间"。
        now = timezone.now()
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
                    'created_at': now,
                },
            )
        evaluated = sum(1 for r in results if r.get('score', 0) > 0)
        logger.info(
            f'[ProdEval] 评估完成 qa_id={qa_id}, 成功维度={evaluated}/{len(results)}',
        )
        return {
            'ok': True,
            'evaluated': evaluated,
            'total': len(results),
            'eval_batch_id': eval_batch_id,
        }
    except Exception as e:
        logger.exception(f'[ProdEval] 评估失败 qa_id={qa_id}: {e}')
        return {'ok': False, 'reason': f'eval_failed: {e}', 'eval_batch_id': eval_batch_id}
