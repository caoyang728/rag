"""低分对话归因分析 - 规则归因 + 模板建议 + LLM 个性化建议

设计思路(详见前序讨论):
- 规则归因为主(零 LLM 成本、可解释、可审计),基于已有信号:
  MultiDimensionScore 12 维分数 + reason + QaRecord.retrieval_scores 结构
- 模板建议兜底(每类原因预置标准建议),覆盖 80% 场景
- LLM 个性化建议仅对"关键低分"触发(关键维度低分 / 多维低分),控成本
- safety 类不走 LLM,直接告警(建议无意义,要立即人工处置)

分层触发判断:
1. safety 低分 → 模板告警建议,不走 LLM
2. 关键维度(faithfulness/context_relevancy/answer_relevancy/hallucination)低分 → LLM 建议
3. 多维(>=3)低分 → LLM 建议(综合问题需深度分析)
4. 边缘维度(conciseness/clarity/professionalism 等)低分 → 仅模板建议

成本控制:复用 AnalyticsConfig 的 eval_model;LLM 建议一次调用,temperature=0 保证稳定。
"""
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger


# 默认阈值:与 EvalDashboardOverviewView 的 low_score threshold 一致
DEFAULT_THRESHOLD = 0.5
# 触发 LLM 建议的关键维度(这几个维度低分说明 RAG 核心质量出问题,值得花成本深度分析)
CRITICAL_DIMENSIONS = frozenset({
    'faithfulness', 'context_relevancy', 'answer_relevancy', 'hallucination',
})
# 触发 LLM 建议的低分维度数门槛(多维同时低分说明综合问题)
LLM_TRIGGER_DIM_COUNT = 3
# TopK 命中数门槛:少于该值认为召回不足
RECALL_MIN_HITS = 3
# rerank 分门槛:低于该值认为重排失效
RERANK_MIN_SCORE = 0.5


# ============================================================================
# 模板建议库 - 每类原因对应一套标准建议(短期可执行 + 长期优化方向)
# 模板建议看多了会麻木,但胜在稳定可审计;LLM 建议在此基础上做个性化改写
# ============================================================================
TEMPLATE_SUGGESTIONS: Dict[str, Dict[str, List[str]]] = {
    'retrieval_recall': {
        'short_term': [
            '扩大检索 TopK 参数(如 5→10),让更多候选进入重排',
            '检查 BM25 与向量检索的权重配比,适当调高召回方权重',
            '确认该问题对应的知识库 root_type 是否被正确指定',
        ],
        'long_term': [
            '补充该领域文档,扩充知识库覆盖',
            '评估并升级 embedding 模型,提升语义召回能力',
        ],
    },
    'retrieval_rank': {
        'short_term': [
            '调整 rerank 模型阈值或更换更强的 rerank 模型',
            '检查 RRF 融合参数(alpha),平衡向量与 BM25 贡献',
            '审查 TopK 切片内容,确认是否有噪声片段拉低排序',
        ],
        'long_term': [
            '收集该类问题的相关文档标注,微调 rerank 模型',
            '引入 query 改写或扩展,提升初始候选质量',
        ],
    },
    'content_gap': {
        'short_term': [
            '将该问题标记为知识空白,提示用户问题可能超纲',
            '临时调整拒答 prompt,明确告知用户无相关资料',
        ],
        'long_term': [
            '收集该类高频问题,定向补充知识库文档',
            '建立知识空白监测看板,持续跟踪未覆盖查询',
        ],
    },
    'content_quality': {
        'short_term': [
            '检查相关文档的切片大小,过碎或过大都需重新切分',
            '确认文档解析是否完整(表格/图片是否丢失)',
        ],
        'long_term': [
            '优化文档解析器,提升表格/结构化内容提取率',
            '调整切片策略(如按语义边界切分,而非固定长度)',
        ],
    },
    'generation_hallucination': {
        'short_term': [
            '在 QA prompt 中加强"严格基于上下文回答,无依据时明确说明"约束',
            '降低生成温度(temperature 0.3→0.1),减少发散',
            '检查是否存在过度概括或推理超出上下文的表述',
        ],
        'long_term': [
            '引入 faithfulness 后处理校验,生成后做事实核查',
            '优化 prompt 模板,增加"不确定时拒答"的示例',
        ],
    },
    'generation_offtopic': {
        'short_term': [
            '检查 prompt 是否聚焦用户原始问题,避免被 contexts 带偏',
            '确认 retrieval 是否返回了与问题无关的高分片段',
        ],
        'long_term': [
            '优化 query 改写,确保检索意图与用户原始意图一致',
            '在 prompt 中增加"直接回答用户问题,不要扩展无关内容"约束',
        ],
    },
    'generation_incomplete': {
        'short_term': [
            '提高 max_tokens 上限,避免回答被截断',
            '在 prompt 中要求"覆盖问题所有关键方面"',
        ],
        'long_term': [
            '引入 answer completeness 校验,识别遗漏要点',
            '针对多要点问题,改用分点作答的 prompt 模板',
        ],
    },
    'generation_format': {
        'short_term': [
            '在 prompt 中明确输出格式要求(如分点、加粗关键信息)',
            '要求回答简洁直接,避免冗余铺垫',
        ],
        'long_term': [
            '收集格式差评样本,迭代 prompt 模板',
            '对长回答引入摘要后处理',
        ],
    },
    'safety': {
        # safety 类不走 LLM,模板即为告警处置建议
        'short_term': [
            '【告警】立即人工复核该对话,确认是否触发安全策略',
            '审查相关 contexts 是否包含敏感内容,必要时下架文档',
            '检查安全过滤链路是否被绕过',
        ],
        'long_term': [
            '扩充敏感词库 / 安全分类器训练数据',
            '在生成前后增加双重的安全过滤',
        ],
    },
    'question_side': {
        'short_term': [
            '问题本身模糊或超纲,非系统质量问题',
            '建议引导用户澄清问题或缩小范围',
        ],
        'long_term': [
            '收集高频模糊问题,补充澄清式 prompt',
            '在拒答流程中提供"相关问题建议"引导',
        ],
    },
    'unknown': {
        'short_term': [
            '规则未能归因,建议人工排查该对话',
            '检查 12 维评估 reason 字段获取更多线索',
        ],
        'long_term': [
            '将此类样本沉淀为新的归因规则,持续完善决策树',
        ],
    },
}


# 归因分类 → 受影响层级的映射(前端展示 + 统计分组用)
CATEGORY_TO_LAYER: Dict[str, str] = {
    'retrieval_recall': 'retrieval',
    'retrieval_rank': 'retrieval',
    'content_gap': 'content',
    'content_quality': 'content',
    'generation_hallucination': 'generation',
    'generation_offtopic': 'generation',
    'generation_incomplete': 'generation',
    'generation_format': 'generation',
    'safety': 'safety',
    'question_side': 'question',
    'unknown': 'unknown',
}


def _get_low_dimensions(scores: List[Dict[str, Any]], threshold: float) -> List[Dict[str, Any]]:
    """从 12 维评分中筛出低于阈值的维度,按分数升序

    Args:
        scores: MultiDimensionScore 的序列化列表 [{dimension, score, reason}]
        threshold: 低分阈值

    Returns:
        [{dimension, score, reason}] 升序,空列表表示无低分维度
    """
    low = [
        {
            'dimension': s.get('dimension', ''),
            'score': round(float(s.get('score') or 0), 4),
            'reason': str(s.get('reason') or '')[:200],
        }
        for s in scores
        if float(s.get('score') or 0) < threshold
    ]
    low.sort(key=lambda x: x['score'])
    return low


def _get_retrieval_signal(qa_record) -> Dict[str, Any]:
    """从 QaRecord.retrieval_scores 提取检索链路信号

    retrieval_scores 结构: [{chunk_id, vector, bm25, rrf, rerank}]
    用于区分"召回不足"vs"排序失效":
    - 命中切片数少(<3) → 召回不足
    - 命中切片数多但 rerank 分低 → 排序失效

    Returns:
        {hit_count, max_rerank, avg_rerank, has_context}
    """
    hits = qa_record.retrieval_scores or []
    if not hits:
        return {'hit_count': 0, 'max_rerank': 0.0, 'avg_rerank': 0.0, 'has_context': False}

    rerank_scores = []
    for h in hits:
        # 兼容字段名:rerank / rerank_score
        r = h.get('rerank')
        if r is None:
            r = h.get('rerank_score')
        try:
            rerank_scores.append(float(r) if r is not None else 0.0)
        except (TypeError, ValueError):
            continue

    if not rerank_scores:
        return {'hit_count': len(hits), 'max_rerank': 0.0, 'avg_rerank': 0.0, 'has_context': len(hits) > 0}

    return {
        'hit_count': len(hits),
        'max_rerank': round(max(rerank_scores), 4),
        'avg_rerank': round(sum(rerank_scores) / len(rerank_scores), 4),
        'has_context': True,
    }


def _rule_based_root_cause(
    scores: List[Dict[str, Any]],
    low_dims: List[Dict[str, Any]],
    qa_record,
    retrieval_signal: Dict[str, Any],
    threshold: float,
) -> Tuple[str, str]:
    """规则归因决策树 - 按优先级匹配第一个命中的规则

    优先级设计(从高到低):
    1. safety: 安全问题最高优先级,必须立即处置
    2. content_gap: 无 contexts 且拒答 → 知识盲区
    3. generation_hallucination: faithfulness 低 + 检索好 → 生成层幻觉
    4. retrieval_recall: context_relevancy 低 + 命中少 → 召回不足
    5. retrieval_rank: context_relevancy 低 + 命中多但 rerank 低 → 排序失效
    6. generation_offtopic: answer_relevancy 低 + 有 contexts → 生成跑题
    7. question_side: answer_relevancy 低 + 无 contexts → 问题超纲
    8. generation_incomplete: completeness 低
    9. generation_format: clarity/conciseness 低
    10. unknown: 兜底

    Returns:
        (category, detail) detail 为规则命中的具体条件描述
    """
    # 维度分数字典,便于按名查询
    dim_score = {d['dimension']: d['score'] for d in low_dims}
    low_dim_names = set(dim_score.keys())

    # 1. safety 最高优先级
    if 'toxicity' in low_dim_names or 'bias' in low_dim_names:
        bad = [d for d in ('toxicity', 'bias') if d in low_dim_names]
        return 'safety', f'安全维度低分: {",".join(bad)} < {threshold}'

    # 2. 无检索上下文 + 拒答 → 知识盲区
    if not retrieval_signal['has_context']:
        if getattr(qa_record, 'answer_type', '') == 'refused':
            return 'content_gap', '无检索上下文且系统拒答,判定为知识盲区'
        # 有问题但无 contexts 且未拒答,倾向问题超纲
        if 'answer_relevancy' in low_dim_names:
            return 'question_side', '无检索上下文且回答相关性低,问题可能超纲'

    # 3. faithfulness 低 + 检索好 → 生成幻觉
    if 'faithfulness' in low_dim_names and retrieval_signal['max_rerank'] >= RERANK_MIN_SCORE:
        return (
            'generation_hallucination',
            f'faithfulness={dim_score["faithfulness"]:.2f} 低但 rerank={retrieval_signal["max_rerank"]:.2f} 高,'
            f'检索质量良好,幻觉源于生成层',
        )

    # 4. context_relevancy 低 + 命中少 → 召回不足
    if 'context_relevancy' in low_dim_names and retrieval_signal['hit_count'] < RECALL_MIN_HITS:
        return (
            'retrieval_recall',
            f'context_relevancy={dim_score["context_relevancy"]:.2f} 低且命中切片数'
            f'{retrieval_signal["hit_count"]}<{RECALL_MIN_HITS},召回不足',
        )

    # 5. context_relevancy 低 + 命中多但 rerank 低 → 排序失效
    if 'context_relevancy' in low_dim_names and retrieval_signal['hit_count'] >= RECALL_MIN_HITS \
            and retrieval_signal['max_rerank'] < RERANK_MIN_SCORE:
        return (
            'retrieval_rank',
            f'context_relevancy={dim_score["context_relevancy"]:.2f} 低,命中'
            f'{retrieval_signal["hit_count"]} 片段但 rerank={retrieval_signal["max_rerank"]:.2f} 低,排序失效',
        )

    # 6. answer_relevancy 低 + 有 contexts → 生成跑题
    if 'answer_relevancy' in low_dim_names and retrieval_signal['has_context']:
        return (
            'generation_offtopic',
            f'answer_relevancy={dim_score["answer_relevancy"]:.2f} 低但存在检索上下文,生成跑题',
        )

    # 7. answer_relevancy 低 + 无 contexts → 问题超纲
    if 'answer_relevancy' in low_dim_names and not retrieval_signal['has_context']:
        return 'question_side', '回答相关性低且无检索上下文,问题可能超纲'

    # 8. completeness 低 → 生成不完整
    if 'completeness' in low_dim_names:
        return 'generation_incomplete', f'completeness={dim_score["completeness"]:.2f} 低,回答不完整'

    # 9. clarity 或 conciseness 低 → 生成表达差
    if 'clarity' in low_dim_names or 'conciseness' in low_dim_names:
        bad = [d for d in ('clarity', 'conciseness') if d in low_dim_names]
        return 'generation_format', f'表达类维度低分: {",".join(bad)}'

    # 10. 兜底
    return 'unknown', f'规则未命中明确分类,低分维度: {",".join(low_dim_names) or "无"}'


def _should_trigger_llm(category: str, low_dims: List[Dict[str, Any]]) -> bool:
    """判断是否需要触发 LLM 生成个性化建议

    分层策略:
    - safety: 不走 LLM(直接告警,建议无意义)
    - question_side: 不走 LLM(问题侧,LLM 无用武之地)
    - unknown: 不走 LLM(规则未归因,LLM 也难有针对性)
    - 关键维度低分: 走 LLM
    - 多维(>=3)低分: 走 LLM
    - 其他单维度边缘低分: 仅模板(返回 False)
    """
    if category in ('safety', 'question_side', 'unknown'):
        return False

    low_dim_names = {d['dimension'] for d in low_dims}
    # 关键维度低分
    if low_dim_names & CRITICAL_DIMENSIONS:
        return True
    # 多维低分
    if len(low_dim_names) >= LLM_TRIGGER_DIM_COUNT:
        return True
    return False


def _build_template_suggestions(category: str) -> List[Dict[str, str]]:
    """从模板库取该类原因的标准建议,转为统一结构"""
    tpl = TEMPLATE_SUGGESTIONS.get(category, TEMPLATE_SUGGESTIONS['unknown'])
    suggestions = []
    for action in tpl.get('short_term', []):
        suggestions.append({'type': 'short_term', 'action': action})
    for action in tpl.get('long_term', []):
        suggestions.append({'type': 'long_term', 'action': action})
    return suggestions


def _build_llm_prompt(
    qa_record,
    low_dims: List[Dict[str, Any]],
    category: str,
    detail: str,
    retrieval_signal: Dict[str, Any],
) -> List[Dict[str, str]]:
    """构建 LLM 归因建议 prompt

    输入包含:归因分类 + 低分维度 reason + 检索信号 + 问题/回答摘要 + TopK contexts
    要求输出结构化 JSON,避免自由发挥(便于落库 + 前端展示)
    """
    # contexts 截断控制 token:每片 300 字,最多 3 片
    from apps.analytics.services.production_eval_service import build_context_list
    contexts = build_context_list(qa_record)[:3]
    contexts_text = '\n---\n'.join(c[:300] for c in contexts) if contexts else '(无检索上下文)'

    # 低分维度 reason 拼接(给 LLM 看 DeepEval 的判断依据)
    low_dims_text = '\n'.join(
        f'- {d["dimension"]}(score={d["score"]:.2f}): {d["reason"]}'
        for d in low_dims[:6]  # 最多 6 个,控制 token
    ) or '(无低分维度 reason)'

    # 问题/回答截断
    question = (qa_record.question or '')[:300]
    answer = (qa_record.answer or '')[:500]

    system_prompt = (
        '你是 RAG 系统的质量分析专家。根据低分对话的归因分类、评估理由和检索信号,'
        '给出针对性、可执行的优化建议。建议必须落到具体动作(调什么参数、改什么 prompt、补什么文档),'
        '避免"加强优化""提升质量"这类正确废话。'
    )

    user_prompt = f"""已完成的规则归因:
- 归因分类: {category}
- 命中规则: {detail}
- 检索信号: 命中切片 {retrieval_signal['hit_count']} 个, rerank 最高 {retrieval_signal['max_rerank']}

低分维度及 DeepEval 评估理由:
{low_dims_text}

用户问题:
{question}

系统回答(摘要):
{answer}

检索上下文(片段):
{contexts_text}

请基于以上信息,输出 JSON(仅输出 JSON,不要 markdown 代码块):
{{
  "diagnosis": "一句话诊断,点明根因",
  "short_term_actions": ["短期可立即执行的动作1", "动作2"],
  "long_term_actions": ["长期优化方向1"]
}}

要求:
1. diagnosis 不超过 50 字,直接点明根因
2. short_term_actions 2-3 条,每条不超过 40 字,必须可立即执行
3. long_term_actions 1-2 条,每条不超过 40 字
4. 建议要针对这一条对话的具体情况,不要给通用建议"""

    return [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': user_prompt},
    ]


def _parse_llm_response(content: str) -> Optional[Dict[str, Any]]:
    """解析 LLM 返回的 JSON 建议响应

    LLM 可能包裹在 markdown 代码块中,需剥离。
    解析失败返回 None,调用方降级为模板建议。
    """
    if not content:
        return None
    text = content.strip()
    # 剥离 markdown 代码块
    if text.startswith('```'):
        # 去掉首行 ``` 或 ```json
        lines = text.split('\n')
        if len(lines) >= 2:
            lines = lines[1:]
            if lines and lines[-1].strip().startswith('```'):
                lines = lines[:-1]
            text = '\n'.join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    # 字段校验 + 兜底
    return {
        'diagnosis': str(data.get('diagnosis') or '')[:200],
        'short_term_actions': [
            str(a)[:100] for a in data.get('short_term_actions', []) if a
        ][:3],
        'long_term_actions': [
            str(a)[:100] for a in data.get('long_term_actions', []) if a
        ][:2],
    }


def _llm_generate_suggestions(
    qa_record,
    low_dims: List[Dict[str, Any]],
    category: str,
    detail: str,
    retrieval_signal: Dict[str, Any],
    model: str,
) -> Tuple[str, List[Dict[str, str]], int, float, int]:
    """调用 LLM 生成个性化建议

    失败时降级为模板建议(不让 LLM 故障阻塞归因流程)。

    Returns:
        (diagnosis, suggestions, tokens_used, cost, latency_ms)
    """
    from apps.llm.factory import get_llm

    t0 = time.time()
    try:
        llm = get_llm(model=model)
        messages = _build_llm_prompt(qa_record, low_dims, category, detail, retrieval_signal)
        resp = llm.chat(messages, temperature=0, max_tokens=800)

        parsed = _parse_llm_response(resp.get('content', ''))
        if not parsed or (not parsed['short_term_actions'] and not parsed['long_term_actions']):
            # LLM 返回不可用,降级模板
            logger.warning('[LowScoreAnalysis] LLM 响应解析失败,降级模板建议')
            return '', _build_template_suggestions(category), 0, 0.0, int((time.time() - t0) * 1000)

        # 合并:LLM 建议为主,模板建议作为补充兜底
        suggestions = []
        for action in parsed['short_term_actions']:
            suggestions.append({'type': 'short_term', 'action': action})
        for action in parsed['long_term_actions']:
            suggestions.append({'type': 'long_term', 'action': action})

        tokens = int(resp.get('total_tokens') or 0)
        cost = float(resp.get('cost') or 0)
        latency_ms = int(resp.get('latency_ms') or (time.time() - t0) * 1000)
        return parsed['diagnosis'], suggestions, tokens, cost, latency_ms
    except Exception as e:
        logger.warning(f'[LowScoreAnalysis] LLM 建议生成异常,降级模板: {e}')
        return '', _build_template_suggestions(category), 0, 0.0, int((time.time() - t0) * 1000)


def analyze_low_score_qa(
    qa_record_id: int,
    scores: Optional[List[Dict[str, Any]]] = None,
    threshold: float = DEFAULT_THRESHOLD,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """对单条低分 QA 执行归因分析 + 建议生成

    主入口:规则归因 → 判断是否走 LLM → 生成建议 → 返回结构化结果。
    调用方(Celery 任务)负责落库 LowScoreScore.objects.update_or_create。

    Args:
        qa_record_id: QaRecord.id
        scores: 12 维评分列表;None 时从 DB 查询(避免重复查询)
        threshold: 低分阈值,默认 0.5
        model: LLM 模型名;None 用 AnalyticsConfig.eval_model()

    Returns:
        {
            'category', 'detail', 'affected_layer', 'low_dimensions',
            'diagnosis', 'suggestions', 'method', 'model',
            'tokens', 'cost', 'latency_ms', 'avg_score'
        }
        失败时抛异常,由调用方捕获落 failed 状态
    """
    from apps.analytics.models import QaRecord, MultiDimensionScore
    from rag_project.config import AnalyticsConfig

    qa = QaRecord.objects.get(id=qa_record_id)

    # 12 维评分:外部传入优先(避免重复查询),否则从 DB 取
    if scores is None:
        scores = list(
            MultiDimensionScore.objects
            .filter(qa_record_id=qa_record_id)
            .values('dimension', 'score', 'reason')
        )

    if not scores:
        raise ValueError(f'QA {qa_record_id} 无评估分数,无法归因')

    avg_score = sum(float(s.get('score') or 0) for s in scores) / len(scores)
    low_dims = _get_low_dimensions(scores, threshold)

    if not low_dims:
        # 无低分维度,不应触发归因(调用方应过滤),这里兜底返回 unknown
        return {
            'category': 'unknown',
            'detail': '无低分维度,无需归因',
            'affected_layer': 'unknown',
            'low_dimensions': [],
            'diagnosis': '',
            'suggestions': [],
            'method': 'rule',
            'model': '',
            'tokens': 0, 'cost': 0.0, 'latency_ms': 0,
            'avg_score': round(avg_score, 4),
        }

    # 检索信号
    retrieval_signal = _get_retrieval_signal(qa)

    # 规则归因
    category, detail = _rule_based_root_cause(scores, low_dims, qa, retrieval_signal, threshold)
    affected_layer = CATEGORY_TO_LAYER.get(category, 'unknown')

    # 判断是否走 LLM
    use_llm = _should_trigger_llm(category, low_dims)
    llm_model = model or AnalyticsConfig.eval_model()

    if use_llm:
        diagnosis, suggestions, tokens, cost, latency_ms = _llm_generate_suggestions(
            qa, low_dims, category, detail, retrieval_signal, llm_model,
        )
        method = 'hybrid'  # 规则归因 + LLM 建议
        used_model = f'deepeval-{llm_model}'
    else:
        diagnosis = ''
        suggestions = _build_template_suggestions(category)
        tokens, cost, latency_ms = 0, 0.0, 0
        method = 'rule'
        used_model = ''

    return {
        'category': category,
        'detail': detail,
        'affected_layer': affected_layer,
        'low_dimensions': low_dims,
        'diagnosis': diagnosis,
        'suggestions': suggestions,
        'method': method,
        'model': used_model,
        'tokens': tokens,
        'cost': cost,
        'latency_ms': latency_ms,
        'avg_score': round(avg_score, 4),
    }
