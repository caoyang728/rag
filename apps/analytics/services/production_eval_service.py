"""
生产对话自动评估 —— 采样率 + 分层限速 + 异步评估

对话结束后按"采样率 + 分层限速"策略,异步触发 DeepEval 12 维评估
(evaluate_with_deepeval),结果落 MultiDimensionScore。与定时批量任务
run_multi_dimension_evaluation 互补:即时路径负责采样,批量负责回扫未覆盖项。

评估入口策略(maybe_dispatch_eval):
1. 采样率:按配置比例随机抽取对话进行评估(默认 5%),0=不评估,1=全量评估
2. 分钟限速:Redis 原子 INCR,每分钟最多 rate_per_min 个评估(默认 5)
3. 小时限速:Redis 原子 INCR,每小时最多 rate_per_hour 个评估(默认 50)
4. 日预算:每日最多 eval_daily_limit 条 + 成本不超过 eval_cost_limit 元

成本保护:
1. 采样率(默认 5%):入口拦截,控制评估触发量
2. 令牌桶(默认 5/min + 50/h):Redis 原子 INCR,分层限速
3. 日限(默认 500/日)+ 成本限(默认 1 元/日):Redis 日计数 + DB 成本聚合
"""
import random
import time
from typing import List, Optional, Tuple

from celery import shared_task
from django.db.models import Sum
from django.utils import timezone
from loguru import logger


def build_context_list(qa_record) -> List[str]:
    """从 QaRecord 构建 DeepEval 评估用的检索上下文，返回 list[str]

    按路由来源分流（三层路由回答的 retrieval_scores 为空，需按来源重建上下文）：
    - route_source='wiki'：重新检索 Wiki 页面，取页面正文
    - route_source 以 'graphrag' 开头：重跑图谱检索，取图谱上下文
    - 其他（rag/无路由）：取 retrieval_scores 里的切片内容（Top5，每片截断 500 字）

    DeepEval 的 FaithfulnessMetric/ContextualRelevancyMetric 需要 retrieval_context
    为 list[str](每片独立)。
    """
    route_source = getattr(qa_record, 'route_source', None) or ''
    if route_source == 'wiki':
        return _build_wiki_route_context(qa_record.question)
    if route_source.startswith('graphrag'):
        return _build_graphrag_route_context(qa_record.question, qa_record.user_id)

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


def _build_wiki_route_context(question: str) -> List[str]:
    """Wiki 路由回答的评估上下文：按问题重新检索 Wiki 页面

    路由回答未落 retrieval_scores（wiki 层 chunks 为空），评估时按问题
    重新检索命中页面，用页面正文作为 retrieval_context。
    检索阈值与路由参与阈值(0.55)对齐，低于该值视为重建失败。
    """
    from apps.wiki.retriever import search_wiki

    try:
        results = search_wiki(question, top_k=1, threshold=0.55)
    except Exception as e:
        logger.warning(f'[ProdEval] wiki 上下文重建失败: {e}')
        return []
    if not results:
        return []
    content = (results[0].get('content', '') or '').strip()
    if not content:
        return []
    return [content[:500]]


def _build_graphrag_route_context(question: str, user_id: int) -> List[str]:
    """GraphRAG 路由回答的评估上下文：重跑图谱检索取上下文

    graphrag_search 需要 user 做权限过滤；原提问用户已删除时回退系统用户，
    检索失败或无上下文返回空列表（评估任务会跳过该 QA）。
    """
    from apps.graph.retriever import graphrag_search
    from apps.users.models import User

    user = None
    if user_id:
        try:
            user = User.objects.filter(id=user_id).first()
        except Exception:
            user = None
    if user is None:
        # 兜底用系统用户（评估只看内容相关性，不涉及具体权限边界）
        user = User.objects.filter(username='system').first()
    if user is None:
        return []
    try:
        result = graphrag_search(question, user, mode='auto')
    except Exception as e:
        logger.warning(f'[ProdEval] graphrag 上下文重建失败: {e}')
        return []
    context = (result.get('context', '') or '').strip()
    if not context:
        return []
    return [context[:500]]


def get_redis():
    """复用 Analytics 专用 Redis 连接(DB 3),令牌桶与日计数共用"""
    from apps.analytics.services.realtime_service import get_redis_safe
    return get_redis_safe()


def _acquire_token(rate_per_min: int) -> bool:
    """令牌桶限速:每分钟最多 rate_per_min 个评估

    实现方式:Redis 计数器,key 带分钟时间戳,INCR 后首次 EXPIRE 65s(略大于 60s 容错)。
    INCR 是原子操作,多 worker 并发下也能精确计数。
    超限返回 False,调用方跳过本次评估。

    Redis 故障时保守返回 False:宁可少评估也不打爆 LLM 评估接口。
    """
    try:
        r = get_redis()
        minute_key = f'analytics:eval_rate:{int(time.time() // 60)}'
        count = r.incr(minute_key)
        if count == 1:
            r.expire(minute_key, 65)
        return count <= rate_per_min
    except Exception as e:
        logger.warning(f'[ProdEval] 令牌桶 Redis 异常,保守跳过: {e}')
        return False


def _acquire_hourly_token(rate_per_hour: int) -> bool:
    """小时级令牌桶限速:每小时最多 rate_per_hour 个评估

    与每分钟令牌桶配合做分层限速:分钟级防止突发,小时级控制总量。
    实现方式与 _acquire_token 一致,key 带小时时间戳,EXPIRE 3700s(略大于 3600s 容错)。
    """
    try:
        r = get_redis()
        hour_key = f'analytics:eval_rate_hourly:{int(time.time() // 3600)}'
        count = r.incr(hour_key)
        if count == 1:
            r.expire(hour_key, 3700)
        return count <= rate_per_hour
    except Exception as e:
        logger.warning(f'[ProdEval] 小时限速 Redis 异常,保守跳过: {e}')
        return False


def check_daily_budget(r, occupy: bool = True) -> Tuple[bool, str]:
    """日预算检查:数量上限 + 成本上限

    数量上限用 Redis 日计数器(原子,INCR 后超限回退,不占配额);
    成本上限从 MultiDimensionScore 聚合今日 eval_cost(非原子,但成本是软限制,近似即可)。

    occupy=False 时只读不占,供入口预检使用;配额由评估任务内真正占用一次,
    避免"入口预占 + 任务再占"导致日评估量实际只有配额一半(H3)。

    Returns:
        (是否通过, 拒绝原因)
    """
    from rag_project.config import AnalyticsConfig

    daily_limit = AnalyticsConfig.eval_daily_limit()
    cost_limit = AnalyticsConfig.eval_cost_limit()

    # 数量限:Redis 原子计数
    # 用本地业务日期做日界(timezone.localdate),与项目其他日预算实现保持一致；
    # 否则本地 00:00-08:00 期间 strftime("%Y%m%d") 取的是 UTC 昨天,日限跨天错位
    day_key = f'analytics:eval_daily:{timezone.localdate().strftime("%Y%m%d")}'
    if occupy:
        count = r.incr(day_key)
        if count == 1:
            r.expire(day_key, 90000)  # 25h,跨天自动清理
        if count > daily_limit:
            r.decr(day_key)  # 超限回退,不占用配额
            return False, 'daily_limit_exceeded'
    else:
        # 入口预检只读当前计数,不占配额,避免与任务内占用重复计数
        if int(r.get(day_key) or 0) >= daily_limit:
            return False, 'daily_limit_exceeded'

    # 成本限:DB 聚合今日已发生成本
    try:
        from apps.analytics.models import MultiDimensionScore
        # 本地业务日零点(与数量限同口径),避免 UTC 截断导致成本限跨天错位
        today_start = timezone.make_aware(
            timezone.datetime.combine(timezone.localdate(), timezone.datetime.min.time())
        )
        total_cost = MultiDimensionScore.objects.filter(
            created_at__gte=today_start,
        ).aggregate(total=Sum('eval_cost'))['total'] or 0
        if float(total_cost) >= cost_limit:
            r.decr(day_key)  # 回退
            return False, 'cost_limit_exceeded'
    except Exception as e:
        logger.warning(f'[ProdEval] 成本聚合查询失败,仅按数量限: {e}')

    return True, ''


def save_eval_results(qa_id: int, results: List[dict], eval_model: str, eval_batch_id: str,
                      atomic_facts: Optional[list] = None) -> int:
    """将 DeepEval 多维度评估结果逐维度幂等落库(MultiDimensionScore)

    生产采样评估(evaluate_sampled_qa)与批量回扫(run_multi_dimension_evaluation)
    共用,统一三点逻辑:
    - update_or_create 幂等:同 qa+维度只保留最新一次评估
    - created_at 显式写入:auto_now_add 在 UPDATE 分支不生效,手动覆盖使看板
      时间窗口过滤到重新评估的最新记录
    - eval_cost 按维度实际 tokens 折算(元)落库,日成本聚合不遗漏评估消耗

    atomic_facts 为 None 时不写入该字段(批量任务无此数据),显式传入空列表
    表示写入空集(生产采样场景与历史行为一致)。

    Returns:
        成功维度数(score > 0 的维度个数)
    """
    from decimal import Decimal

    from apps.analytics.models import MultiDimensionScore
    from apps.llm.providers.deepseek import DEEPSEEK_PRICING

    now = timezone.now()
    # 按维度实际 tokens 估算成本(元):deepeval_service 已回填真实 tokens,
    # 旧返回值缺字段时回退 tokens_used/0
    pricing = DEEPSEEK_PRICING.get(eval_model, DEEPSEEK_PRICING['deepseek-chat'])
    evaluated = 0
    for res in results:
        in_tokens = res.get('input_tokens', 0) or 0
        out_tokens = res.get('output_tokens', 0) or 0
        # 成本 = prompt tokens * 单价 + completion tokens * 单价,单价为元/1K,除以 1000 换元
        cost = (in_tokens * pricing['prompt'] + out_tokens * pricing['completion']) / 1000
        defaults = {
            'score': res['score'],
            'reason': res['reason'],
            'eval_model': f'deepeval-{eval_model}',
            'eval_tokens_used': res.get('tokens_used', in_tokens + out_tokens),
            'eval_cost': Decimal(str(round(cost, 6))),
            'eval_latency_ms': res.get('latency_ms', 0),
            'eval_batch_id': eval_batch_id,
            'status': 'completed',
            'created_at': now,
        }
        if atomic_facts is not None:
            defaults['atomic_facts'] = atomic_facts
        MultiDimensionScore.objects.update_or_create(
            qa_record_id=qa_id,
            dimension=res['dimension'],
            defaults=defaults,
        )
        if res.get('score', 0) > 0:
            evaluated += 1
    return evaluated


def maybe_dispatch_eval(qa_record) -> None:
    """生产对话评估入口:在 QaRecord 持久化后调用

    流程:开关 → 过滤无效对话 → 保底评估/采样兜底 → 令牌桶 → dispatch Celery 任务
    任一环节不通过都静默跳过(不影响主对话流程)。

    过滤规则:
    - is_success=False:链路中断,无有效回答,不评估
    - answer_type='refused':正常拒答(无相关资料),无评估意义
    - 缓存命中:回答复用历史,评估重复无价值

    评估入口策略(采样 + 分层限速):
    - 采样率:按配置比例随机抽取对话进行评估,0=不评估,1=全量评估
    - 分层限速:每分钟 + 每小时 + 每日三级令牌桶,防止打爆 LLM 评估接口
    - 成本保护:每日成本上限,超出后停止评估

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

        # 采样率检查:按比例随机抽取,未命中则跳过
        sample_rate = AnalyticsConfig.production_eval_sample_rate()
        if random.random() >= sample_rate:
            return

        # 分层限速:分钟级 → 小时级 → 日预算
        # 分钟级:防止突发请求打爆 LLM 评估接口
        if not _acquire_token(AnalyticsConfig.production_eval_rate_per_min()):
            logger.debug(f'[ProdEval] 分钟限速,跳过 qa_id={qa_record.id}')
            return

        # 小时级:控制每小时评估总量,与分钟级配合做分层限速
        if not _acquire_hourly_token(AnalyticsConfig.production_eval_rate_per_hour()):
            logger.debug(f'[ProdEval] 小时限速,跳过 qa_id={qa_record.id}')
            return

        # 日预算检查:数量上限 + 成本上限(入口只读不占,配额由评估任务内占用一次)
        try:
            r = get_redis()
            passed, reason = check_daily_budget(r, occupy=False)
            if not passed:
                logger.debug(f'[ProdEval] 日预算超限({reason}),跳过 qa_id={qa_record.id}')
                return
        except Exception as e:
            logger.warning(f'[ProdEval] 日预算检查异常,保守跳过: {e}')
            return

        # dispatch 异步评估任务
        evaluate_sampled_qa.delay(qa_record.id)
        logger.info(f'[ProdEval] 已派发评估任务(采样) qa_id={qa_record.id}')
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
    实际启用维度由 EVAL_DISPLAY_DIMENSIONS 控制(默认 12 维,评估=展示强绑定)。

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
    from apps.analytics.models import QaRecord
    from apps.analytics.services.deepeval_service import evaluate_with_deepeval
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
            r = get_redis()
            passed, reason = check_daily_budget(r)
            if not passed:
                return {'ok': False, 'skipped': True, 'reason': reason}
        except Exception as e:
            logger.warning(f'[ProdEval] 日预算检查异常,继续评估: {e}')

    # 构建检索上下文 list(DeepEval retrieval_context 需要 list[str])
    contexts = build_context_list(qa)
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
        # 逐维度幂等落库(同 qa+维度只保留最新一次评估),与批量回扫共用 save_eval_results
        evaluated = save_eval_results(qa_id, results, eval_model, eval_batch_id, atomic_facts=[])
        logger.info(
            f'[ProdEval] 评估完成 qa_id={qa_id}, 成功维度={evaluated}/{len(results)}',
        )

        # 评估成功后自动派发低分归因分析(异步,不阻塞评估返回)
        # 惰性导入避免 production_eval ↔ tasks 循环依赖(tasks 顶部已导入本模块)
        # 归因任务内部会判断均分是否 < threshold,达标才真正分析
        try:
            from apps.analytics.tasks import run_low_score_analysis
            run_low_score_analysis.delay(qa_id)
        except Exception as e:
            logger.warning(f'[ProdEval] 派发低分归因失败(忽略): {e}')

        return {
            'ok': True,
            'evaluated': evaluated,
            'total': len(results),
            'eval_batch_id': eval_batch_id,
        }
    except Exception as e:
        logger.exception(f'[ProdEval] 评估失败 qa_id={qa_id}: {e}')
        return {'ok': False, 'reason': f'eval_failed: {e}', 'eval_batch_id': eval_batch_id}
