"""
DeepEval 指标评估 —— 接入 DeepSeek,生产对话多维度 LLM-as-judge

作为 production_eval 与 offline_eval 的指标计算后端,提供 12 维 LLM-as-judge 评估。
DeepEval 内置 DeepSeekModel,直接复用项目 LLM_API_KEY;metric.measure 同步调用,
适合在 Celery 任务中执行。

指标体系(生产对话无 reference 场景,共 12 维,分四大类):
- 检索质量(1维):context_relevancy 上下文相关性
- 答案质量(6维):faithfulness 忠实度 / hallucination 幻觉 /
                answer_relevancy 相关性 / completeness 完整性 /
                conciseness 简洁性 / clarity 清晰度
- 安全性(2维):toxicity 毒性 / bias 偏见
- 业务体验(3维):professionalism 专业性 / helpfulness 有用性 /
                actionability 可操作性

选型说明:生产对话无 reference,DeepEval 预置安全类 + G-Eval 可自定义,覆盖更广;
且内置 DeepSeekModel 接入零成本。不引入需要 reference 的指标(context_recall /
answer_correctness 生产对话无标准答案,无法计算)。安全类不达标需立即告警,
业务类用于趋势分析。

成本控制:12 维 = 12 次 LLM 调用/对话,单条约 ¥0.01~0.02;
默认配置下日成本上限 ¥1(采样率 + 令牌桶 + 日限 + 成本限四重保护);
可通过 PRODUCTION_EVAL_METRIC_GROUPS 选择性启用指标组进一步降本。
"""
import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger


def get_deepeval_model(model: Optional[str] = None):
    """构建 DeepEval 评估用 DeepSeek 模型

    复用项目 settings.LLM_API_KEY,DeepSeekModel 内置 DeepSeek API 端点。
    temperature=0 保证评估打分可复现。

    Args:
        model: 模型名;None 用 'deepseek-chat'
    """
    from django.conf import settings
    from deepeval.models import DeepSeekModel

    return DeepSeekModel(
        model=model or 'deepseek-chat',
        api_key=settings.LLM_API_KEY,
        temperature=0,
    )


def _build_preset_metrics(model, include_reason: bool = True) -> List[Tuple[str, Any]]:
    """构建 DeepEval 预置指标(经过验证,实现稳定)

    - faithfulness:回答是否忠于 retrieval_context(无幻觉),RAG 核心指标
    - answer_relevancy:回答是否切中用户问题
    - context_relevancy:检索片段是否与问题相关(衡量检索质量,无需 reference)
    - hallucination:回答是否引入了 context 之外的信息(幻觉检测)
    - toxicity/bias:安全性,企业知识库问答必须把控

    所有预置 metric 都支持 include_reason 和 async_mode 参数。
    async_mode=False:在 Celery 同步任务中避免 event loop 问题。
    """
    from deepeval.metrics import (
        FaithfulnessMetric, AnswerRelevancyMetric,
        ContextualRelevancyMetric, HallucinationMetric,
        ToxicityMetric, BiasMetric,
    )

    kwargs = dict(model=model, include_reason=include_reason, async_mode=False)
    return [
        ('faithfulness', FaithfulnessMetric(threshold=0.5, **kwargs)),
        ('answer_relevancy', AnswerRelevancyMetric(threshold=0.5, **kwargs)),
        ('context_relevancy', ContextualRelevancyMetric(threshold=0.5, **kwargs)),
        ('hallucination', HallucinationMetric(threshold=0.5, **kwargs)),
        ('toxicity', ToxicityMetric(threshold=0.5, **kwargs)),
        ('bias', BiasMetric(threshold=0.5, **kwargs)),
    ]


def _build_geval_metrics(model) -> List[Tuple[str, Any]]:
    """构建 G-Eval 自定义业务指标(补足主观维度)

    G-Eval 通过 criteria + evaluation_params 自定义评估维度,
    DeepSeek 中文能力强,criteria 用中文描述与业务语境一致。
    G-Eval 不支持 include_reason 参数(默认输出 reason)。

    6 个自定义维度覆盖业务体验与答案质量:
    - completeness:完整性,是否覆盖问题所有关键方面
    - conciseness:简洁性,是否冗余啰嗦(与 completeness 互补,避免堆砌)
    - clarity:清晰度,结构是否合理、表达是否无歧义
    - professionalism:专业性,是否符合企业助手身份、术语是否准确
    - helpfulness:有用性,是否真正解决用户问题(比 relevancy 更进一步)
    - actionability:可操作性,是否提供具体可执行的步骤或建议
    """
    from deepeval.metrics import GEval
    from deepeval.test_case import SingleTurnParams

    kwargs = dict(model=model, async_mode=False)
    return [
        ('completeness', GEval(
            name='Completeness',
            criteria='判断回答是否完整覆盖了用户问题的所有关键方面,是否存在遗漏重要信息的情况。',
            evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
            **kwargs,
        )),
        ('conciseness', GEval(
            name='Conciseness',
            criteria='判断回答是否简洁精炼,有无冗余重复、过度铺垫或与问题无关的内容。',
            evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
            **kwargs,
        )),
        ('clarity', GEval(
            name='Clarity',
            criteria='判断回答是否清晰易懂、结构合理、表达无歧义。',
            evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
            **kwargs,
        )),
        ('professionalism', GEval(
            name='Professionalism',
            criteria='判断回答是否专业、准确、符合企业知识库问答助手的身份,语气是否得体,术语使用是否正确。',
            evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
            **kwargs,
        )),
        ('helpfulness', GEval(
            name='Helpfulness',
            criteria='判断回答是否真正解决了用户的问题,提供了有价值的信息或有效的解决方案。',
            evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
            **kwargs,
        )),
        ('actionability', GEval(
            name='Actionability',
            criteria='判断回答是否提供了具体可执行的步骤、建议或操作指引,而非仅给出抽象结论。',
            evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
            **kwargs,
        )),
    ]


def build_production_metrics(model) -> List[Tuple[str, Any]]:
    """构建生产对话评估指标集合(12 维,无 reference)

    返回 [(dimension_name, metric), ...],dimension_name 为落库用的标准化维度名。
    所有 metric 设 async_mode=False:在 Celery 同步任务中避免 event loop 问题,
    串行执行虽慢但稳定;生产采样已限速,延迟可接受。

    指标全集(12 维):
    - 预置(6):faithfulness / answer_relevancy / context_relevancy /
              hallucination / toxicity / bias
    - G-Eval 自定义(6):completeness / conciseness / clarity /
                      professionalism / helpfulness / actionability

    分组与降本:
    通过 PRODUCTION_EVAL_METRIC_GROUPS 环境变量选择启用哪些指标组,
    默认 'all' 启用全部;可选 'core'(预置6维) / 'safety'(安全2维) /
    'quality'(质量4维) / 'business'(业务3维),逗号分隔组合。
    """
    from rag_project.config import AnalyticsConfig

    groups = AnalyticsConfig.production_eval_metric_groups()
    # 'all' 或空配置 = 全部启用
    if not groups or 'all' in groups:
        return _build_preset_metrics(model) + _build_geval_metrics(model)

    metrics: List[Tuple[str, Any]] = []
    if 'core' in groups:
        # 核心质量(预置):忠实度 + 相关性
        from deepeval.metrics import FaithfulnessMetric, AnswerRelevancyMetric
        kwargs = dict(model=model, include_reason=True, async_mode=False)
        metrics.extend([
            ('faithfulness', FaithfulnessMetric(threshold=0.5, **kwargs)),
            ('answer_relevancy', AnswerRelevancyMetric(threshold=0.5, **kwargs)),
        ])
    if 'retrieval' in groups:
        # 检索质量:上下文相关性 + 幻觉
        from deepeval.metrics import ContextualRelevancyMetric, HallucinationMetric
        kwargs = dict(model=model, include_reason=True, async_mode=False)
        metrics.extend([
            ('context_relevancy', ContextualRelevancyMetric(threshold=0.5, **kwargs)),
            ('hallucination', HallucinationMetric(threshold=0.5, **kwargs)),
        ])
    if 'safety' in groups:
        # 安全性:毒性 + 偏见
        from deepeval.metrics import ToxicityMetric, BiasMetric
        kwargs = dict(model=model, include_reason=True, async_mode=False)
        metrics.extend([
            ('toxicity', ToxicityMetric(threshold=0.5, **kwargs)),
            ('bias', BiasMetric(threshold=0.5, **kwargs)),
        ])
    if 'quality' in groups:
        # 答案质量:完整性 + 简洁性 + 清晰度
        from deepeval.metrics import GEval
        from deepeval.test_case import SingleTurnParams
        kwargs = dict(model=model, async_mode=False)
        metrics.extend([
            ('completeness', GEval(
                name='Completeness',
                criteria='判断回答是否完整覆盖了用户问题的所有关键方面,是否存在遗漏重要信息的情况。',
                evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
                **kwargs,
            )),
            ('conciseness', GEval(
                name='Conciseness',
                criteria='判断回答是否简洁精炼,有无冗余重复、过度铺垫或与问题无关的内容。',
                evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
                **kwargs,
            )),
            ('clarity', GEval(
                name='Clarity',
                criteria='判断回答是否清晰易懂、结构合理、表达无歧义。',
                evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
                **kwargs,
            )),
        ])
    if 'business' in groups:
        # 业务体验:专业性 + 有用性 + 可操作性
        from deepeval.metrics import GEval
        from deepeval.test_case import SingleTurnParams
        kwargs = dict(model=model, async_mode=False)
        metrics.extend([
            ('professionalism', GEval(
                name='Professionalism',
                criteria='判断回答是否专业、准确、符合企业知识库问答助手的身份,语气是否得体,术语使用是否正确。',
                evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
                **kwargs,
            )),
            ('helpfulness', GEval(
                name='Helpfulness',
                criteria='判断回答是否真正解决了用户的问题,提供了有价值的信息或有效的解决方案。',
                evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
                **kwargs,
            )),
            ('actionability', GEval(
                name='Actionability',
                criteria='判断回答是否提供了具体可执行的步骤、建议或操作指引,而非仅给出抽象结论。',
                evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
                **kwargs,
            )),
        ])

    # 兜底:配置无效时退回全部(避免因配置错误导致无指标评估)
    if not metrics:
        return _build_preset_metrics(model) + _build_geval_metrics(model)
    return metrics


def evaluate_with_deepeval(
    question: str,
    answer: str,
    contexts: List[str],
    model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """用 DeepEval 12 维指标评估单条对话

    构造 LLMTestCase 后逐指标 measure,返回标准化结果列表。
    单指标失败不中断整体(记录失败原因,该维度记 0 分)。

    Args:
        question: 用户问题
        answer: RAG 生成的回答
        contexts: 检索到的切片列表(Faithfulness/Hallucination/ContextualRelevancy 需要 retrieval_context)
        model: 评估模型;None 用 'deepseek-chat'

    Returns:
        [{dimension, score, reason, latency_ms, tokens_used}]
    """
    from deepeval.test_case import LLMTestCase

    deepeval_model = get_deepeval_model(model)
    metrics = build_production_metrics(deepeval_model)

    # LLMTestCase:retrieval_context 是检索到的切片(DeepEval RAG 指标据此判断忠实度/幻觉/上下文相关性)
    # contexts 为空时传 [''] 避免 DeepEval 抛 None 校验错误,相关指标会得低分
    retrieval_context = contexts if contexts else ['']
    test_case = LLMTestCase(
        input=question,
        actual_output=answer,
        retrieval_context=retrieval_context,
    )
    # DeepEval 4.x HallucinationMetric 需要 test_case.context (单个拼接字符串)
    # 优先从 retrieval_context 拼接,与 FaithfulnessMetric 使用同一数据源
    context_str = '\n\n'.join(retrieval_context)
    # LLMTestCase 支持动态属性赋值;若未来版本收紧 __slots__, measure() 传参兜底
    try:
        test_case.context = context_str
    except AttributeError:
        pass

    # DeepEval 原始分与本项目落库分的语义差异:
    # 本项目统一:1=最好(完全符合),0=最差(完全不符合)
    # DeepEval 安全/检测类指标:0=未检测到(即好情况),1=严重(即坏情况) ← 方向相反,需反转
    # 需反转的 3 个维度: hallucination(幻觉检测) / toxicity(毒性检测) / bias(偏见检测)
    # 例如 hallucination 原始 0 = "无幻觉 = 好" → 反转后存 1.0
    INVERTED_DIMS = frozenset({'hallucination', 'toxicity', 'bias'})

    results: List[Dict[str, Any]] = []
    for dim_name, metric in metrics:
        t0 = time.time()
        try:
            metric.measure(test_case)
            score = float(metric.score) if metric.score is not None else 0.0
            # 反向语义维度:反转分数以匹配项目统一语义(高分=好)
            if dim_name in INVERTED_DIMS:
                score = 1.0 - score
            reason = str(metric.reason or '')
            results.append({
                'dimension': dim_name,
                'score': round(score, 4),
                'reason': reason,
                'latency_ms': int((time.time() - t0) * 1000),
                'tokens_used': 0,  # DeepEval 不直接暴露单指标 token,成本靠日限控制
            })
        except Exception as e:
            logger.warning(f'[DeepEval] 维度 {dim_name} 评估失败: {e}')
            results.append({
                'dimension': dim_name,
                'score': 0.0,
                'reason': f'评估失败: {str(e)[:100]}',
                'latency_ms': int((time.time() - t0) * 1000),
                'tokens_used': 0,
            })

    return results
