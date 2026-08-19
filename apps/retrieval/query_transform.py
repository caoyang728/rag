"""
查询改写/扩展 + 查询分解
- rewrite_query: LLM 改写 + 同义词扩展，提升同义表述召回率
- decompose_query: LLM 拆分为 N 个子查询，处理多意图复杂查询
- search_with_transform: 主链路包装（改写 → 混合检索 → 置信度不足 → 分解 → 逐路召回 → RRF 合并去重 → Rerank）
- build_route_trace: 把改写/分解的输入输出转成 QaRecord.route_trace 审计条目

开关（SystemConfig，风险 normal）：
- QUERY_TRANSFORM_ENABLED: 总开关，关闭时 hybrid_search 行为与现状完全一致
- QUERY_DECOMPOSE_THRESHOLD: 改写后检索置信度阈值，低于该值触发查询分解（默认 0.35）
- QUERY_DECOMPOSE_MAX_SUB: 查询分解最多子查询数（默认 3）

降级策略：LLM 改写/分解失败一律降级为原始 Query，不阻断主流程、不抛异常。
"""
import json
import time
from typing import Any, Dict, List, Optional

from loguru import logger

from apps.llm.factory import get_llm
from apps.llm.prompts import (
    REWRITE_SYSTEM, REWRITE_USER_TEMPLATE,
    DECOMPOSE_SYSTEM, DECOMPOSE_USER_TEMPLATE,
)
from apps.system.config_loader import get_config_value

from .hybrid import rrf_fuse


# ---------------------------------------------------------------------------
# 配置读取（SystemConfig，风险 normal）
# ---------------------------------------------------------------------------

def transform_enabled() -> bool:
    """查询改写/分解总开关，默认开启

    开启后 hybrid_search 内部走改写→分解→并行检索→RRF 合并，单次调用即可覆盖多子查询，
    避免 Agent LLM 多轮串行调用 knowledge_search 导致延迟过高。
    关闭时 hybrid_search 直接走原混合检索，行为与旧版完全一致。
    """
    try:
        return bool(get_config_value('QUERY_TRANSFORM_ENABLED', default=True, value_type='bool'))
    except Exception:
        return True


def _decompose_threshold() -> float:
    """改写后检索置信度阈值，默认 0.35

    改写后结果的置信度低于该值时触发查询分解；0.35 大致对应"改写后无命中片段"。
    """
    try:
        val = get_config_value('QUERY_DECOMPOSE_THRESHOLD', default=0.35, value_type='float')
        return float(val) if val is not None else 0.35
    except (TypeError, ValueError):
        return 0.35


def _max_sub_queries() -> int:
    """最大子查询数，默认 3，防御性限制 1-5（防止过度拆分导致检索延迟过高）"""
    try:
        val = get_config_value('QUERY_DECOMPOSE_MAX_SUB', default=3, value_type='int')
        return max(1, min(int(val), 5))
    except (TypeError, ValueError):
        return 3


# ---------------------------------------------------------------------------
# LLM 输出解析
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> Optional[dict]:
    """容错解析 LLM 输出（兼容 ```json 代码块包裹与前后噪声文本）

    与 task_splitter.maybe_split 的解析策略保持一致：
    先整体 json.loads，失败则剥离 markdown 代码块、再取首个 { 到最后一个 } 的子串。
    解析失败返回 None，由调用方降级处理。
    """
    if not raw:
        return None
    text = raw.strip()
    if text.startswith('```'):
        text = text.strip('`')
        if text.startswith('json'):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start, end = text.find('{'), text.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# 改写 / 分解
# ---------------------------------------------------------------------------

def rewrite_query(query: str) -> Dict[str, Any]:
    """LLM 改写 + 同义词扩展

    Returns:
        {'query': 原始查询, 'rewritten_query': str, 'expansions': [str],
         'changed': bool, 'latency_ms': int, 'ok': bool, 'error': str}

    任何失败（LLM 异常 / 输出非 JSON / 字段缺失）都降级为原始 Query：
    rewritten_query=query, changed=False，本函数永不抛异常、不阻断主流程。
    """
    t0 = time.time()
    result = {
        'query': query,
        'rewritten_query': query,
        'expansions': [],
        'changed': False,
        'latency_ms': 0,
        'ok': False,
        'error': '',
    }
    try:
        llm = get_llm()
        resp = llm.chat(
            [
                {'role': 'system', 'content': REWRITE_SYSTEM},
                {'role': 'user', 'content': REWRITE_USER_TEMPLATE.format(query=query)},
            ],
            temperature=0.0, max_tokens=300,
        )
        data = _extract_json(resp.get('content') or '')
        if not data:
            raise ValueError('rewrite output is not valid json')
        rewritten = str(data.get('rewritten_query') or '').strip()
        if not rewritten:
            raise ValueError('rewrite output missing rewritten_query')
        expansions_raw = data.get('expansions') or []
        expansions = [str(x).strip() for x in expansions_raw if str(x).strip()][:3]
        result.update({
            'rewritten_query': rewritten,
            'expansions': expansions,
            'changed': bool(data.get('changed', rewritten != query)),
            'latency_ms': int((time.time() - t0) * 1000),
            'ok': True,
        })
    except Exception as e:
        # 改写失败必须降级为原始 Query，不阻断主流程
        logger.warning(f'[QueryTransform] rewrite failed, degrade to original query: {e}')
        result['error'] = str(e)[:200]
        result['latency_ms'] = int((time.time() - t0) * 1000)
    return result


def decompose_query(query: str) -> Dict[str, Any]:
    """LLM 拆分为 N 个子查询

    Returns:
        {'query': 原始查询, 'sub_queries': [str], 'need_decompose': bool,
         'latency_ms': int, 'ok': bool, 'error': str}

    失败降级为不分解：need_decompose=False, sub_queries=[]，永不抛异常。
    """
    t0 = time.time()
    result = {
        'query': query,
        'sub_queries': [],
        'need_decompose': False,
        'latency_ms': 0,
        'ok': False,
        'error': '',
    }
    try:
        llm = get_llm()
        resp = llm.chat(
            [
                {'role': 'system', 'content': DECOMPOSE_SYSTEM},
                {'role': 'user', 'content': DECOMPOSE_USER_TEMPLATE.format(query=query)},
            ],
            temperature=0.0, max_tokens=500,
        )
        data = _extract_json(resp.get('content') or '')
        if not data:
            raise ValueError('decompose output is not valid json')
        sub_raw = data.get('sub_queries') or []
        sub_queries = [str(x).strip() for x in sub_raw if str(x).strip()][:_max_sub_queries()]
        result.update({
            'sub_queries': sub_queries,
            # need_decompose 且确有子查询才算分解（LLM 误报 need_decompose=True 但无内容时忽略）
            'need_decompose': bool(data.get('need_decompose', False)) and bool(sub_queries),
            'latency_ms': int((time.time() - t0) * 1000),
            'ok': True,
        })
    except Exception as e:
        # 分解失败降级为不分解，不阻断主流程
        logger.warning(f'[QueryTransform] decompose failed, skip decomposition: {e}')
        result['error'] = str(e)[:200]
        result['latency_ms'] = int((time.time() - t0) * 1000)
    return result


# ---------------------------------------------------------------------------
# 主链路包装
# ---------------------------------------------------------------------------

def _compute_confidence(chunks: List[Dict[str, Any]]) -> float:
    """改写后检索置信度：优先取 Rerank 最高分，无分数时按命中数估算

    Rerank 分数维度缺失（do_rerank=False）时退化为命中数估算，
    口径与 graph.router 保持一致（0.3 + 命中数*0.05）。
    """
    if not chunks:
        return 0.0
    scores = [float(c.get('rerank_score') or 0) for c in chunks]
    best = max(scores)
    if best > 0:
        return min(1.0, best)
    return min(1.0, 0.3 + len(chunks) * 0.05)


def search_with_transform(query: str, user, root_types: Optional[List[str]] = None,
                          node_path_prefix: Optional[str] = None,
                          node_ids: Optional[List[int]] = None,
                          do_rerank: bool = True, **kwargs) -> Dict[str, Any]:
    """查询改写/分解包装后的混合检索（总开关开启时由 hybrid_search 调用）

    流程：
    1. 改写：LLM 改写 + 同义词扩展得到 rewritten_query（失败降级为原始 Query）
    2. 混合检索：用 rewritten_query 走 _search_core 完整链路（向量+BM25+RRF+Rerank）
    3. 置信度判定：改写后结果置信度 < QUERY_DECOMPOSE_THRESHOLD 时触发分解
    4. 分解召回：各子查询独立 _search_core（不做 Rerank 省成本），单路失败不影响整体
    5. 合并重排：改写结果 + 子查询结果 RRF 合并去重，再以原始 query Rerank

    返回结构与 hybrid_search 完全一致（对外契约不变），额外携带：
        'transform': {'enabled', 'rewrite': {...}, 'confidence',
                      'decompose': {...}, 'decomposed', 'latency_ms'}
    供 QaRecord.route_trace 审计与评估看板"改写命中率"统计。
    """
    from .hybrid import _search_core
    from .rerank import rerank_docs
    from django.conf import settings

    t0 = time.time()
    transform: Dict[str, Any] = {'enabled': True}

    # 1. 改写
    rw = rewrite_query(query)
    transform['rewrite'] = {
        'original': query,
        'rewritten_query': rw['rewritten_query'],
        'expansions': rw['expansions'],
        'changed': rw['changed'],
        'ok': rw['ok'],
        'error': rw['error'],
        'latency_ms': rw['latency_ms'],
    }
    search_q = rw['rewritten_query'] or query

    # 2. 改写后混合检索（Rerank 与否与调用方保持一致）
    retrieval = _search_core(search_q, user, root_types=root_types,
                             node_path_prefix=node_path_prefix, node_ids=node_ids,
                             do_rerank=do_rerank, **kwargs)
    chunks = retrieval.get('chunks', [])
    confidence = _compute_confidence(chunks)
    transform['confidence'] = round(confidence, 4)

    # 3. 改写后置信度不足 → 尝试分解
    if confidence < _decompose_threshold():
        dc = decompose_query(query)
        transform['decompose'] = {
            'original': query,
            'need_decompose': dc['need_decompose'],
            'sub_queries': dc['sub_queries'],
            'ok': dc['ok'],
            'error': dc['error'],
            'latency_ms': dc['latency_ms'],
        }
        sub_queries = dc['sub_queries'] if dc['need_decompose'] else []
        if sub_queries:
            # 4. 各子查询逐路召回（不做 Rerank 减少重复精排成本）
            sub_lists = []
            for sq in sub_queries:
                try:
                    sub_ret = _search_core(sq, user, root_types=root_types,
                                           node_path_prefix=node_path_prefix,
                                           node_ids=node_ids, do_rerank=False, **kwargs)
                    sub_lists.append(sub_ret.get('chunks', []))
                except Exception as e:
                    logger.warning(f'[QueryTransform] sub query search failed: {sq!r}: {e}')
            # 5. 改写结果 + 各子查询结果 RRF 合并去重，再以原始 query 精排
            candidate_lists = [lst for lst in [chunks] + sub_lists if lst]
            if candidate_lists:
                fused = rrf_fuse(*candidate_lists, top_k=30)
                if do_rerank:
                    top_n = kwargs.get('rerank_top_k') or settings.RETRIEVAL_RERANK_TOP_K
                    merged = rerank_docs(query, fused, top_k=top_n)
                else:
                    merged = fused
                if merged:
                    retrieval['chunks'] = merged
                    transform['decomposed'] = True

    transform['latency_ms'] = int((time.time() - t0) * 1000)
    retrieval['transform'] = transform
    return retrieval


# ---------------------------------------------------------------------------
# 审计：route_trace
# ---------------------------------------------------------------------------

def build_route_trace(transform: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把改写/分解追踪信息转换为 QaRecord.route_trace 审计条目

    route_trace 结构与 graph.router 保持一致：list of {'layer', ..., 'latency_ms'}，
    供评估看板路由分析统计"改写命中率"。开关关闭（无 transform）时返回空列表。
    个性化检索（PERSONALIZED_RETRIEVAL_ENABLED）的审计信息也挂在 transform 下
    （见 profile._merge_into_transform），这里一并转成 layer=personalization 条目。

    Returns:
        [{'layer': 'query_rewrite', 'query', 'rewritten_query', 'expansions',
          'changed', 'ok', 'error', 'latency_ms'},
         {'layer': 'query_decompose', 'query', 'sub_queries', 'need_decompose',
          'decomposed', 'ok', 'error', 'latency_ms'},  # decompose 仅在触发过时存在
         {'layer': 'personalization', 'applied', 'cold_start', 'weight',
          'adjusted_count', 'reordered', 'top_personalized',
          'personalized_hits', 'profile_domains', 'preferred_root_types',
          'latency_ms'}]  # personalization 仅在个性化链路走过时存在
    """
    trace: List[Dict[str, Any]] = []
    if transform and transform.get('enabled'):
        rw = transform.get('rewrite') or {}
        trace.append({
            'layer': 'query_rewrite',
            'query': rw.get('original', ''),
            'rewritten_query': rw.get('rewritten_query', ''),
            'expansions': rw.get('expansions', []),
            'changed': bool(rw.get('changed', False)),
            'ok': bool(rw.get('ok', False)),
            'error': (rw.get('error') or '')[:200],
            'latency_ms': rw.get('latency_ms', 0),
        })
        dc = transform.get('decompose') or {}
        if dc:
            trace.append({
                'layer': 'query_decompose',
                'query': dc.get('original', ''),
                'sub_queries': dc.get('sub_queries', []),
                'need_decompose': bool(dc.get('need_decompose', False)),
                'decomposed': bool(transform.get('decomposed', False)),
                'ok': bool(dc.get('ok', False)),
                'error': (dc.get('error') or '')[:200],
                'latency_ms': dc.get('latency_ms', 0),
            })
    pz = (transform or {}).get('personalization')
    if pz:
        # 延迟导入避免循环依赖（profile 只依赖 system/memory/chat，不依赖本模块）
        from .profile import build_personalization_route_trace
        trace.extend(build_personalization_route_trace(pz))
    return trace
