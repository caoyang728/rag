"""
三层路由 Orchestrator（Wiki → GraphRAG → RAG 兜底）
- decide_route: 三路并行检索（共享 query 向量），按置信度选择最佳结果
- orchestrate: 编排入口（供 executor 等调用方使用）

并行策略：预计算 query embedding 后，Wiki / GraphRAG / RAG 三路同时执行，
取耗时最长的一路作为总延迟（而非三路之和），预期从 5-6s 降到 2-3s。
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from loguru import logger

# 可配置阈值（后续可迁移到 DB 配置）
# 说明：BGE-M3 的 cosine 相似度分布下，完全一致的标题约 0.75、强相关问题约 0.70，
# 因此 Wiki 直接命中阈值取 0.68，检索参与阈值取 0.55（低于此值视为 Wiki 无关）。
WIKI_DIRECT_HIT_THRESHOLD = 0.68   # Wiki 直接命中：高于此值直接返回
WIKI_SEARCH_THRESHOLD = 0.55       # Wiki 检索最低阈值
GRAPH_CONFIDENCE_THRESHOLD = 0.45  # GraphRAG 接受阈值（实体全相关+有关系的场景约 0.5+）

# 三路并行线程池（3 路检索 + 1 路引用补充）
_route_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='route')


def _citation_chunks(query: str, user, node_ids, root_types,
                     query_vector=None, limit: int = 5) -> List[Dict]:
    """为 Wiki/GraphRAG 命中补充文档引用 chunks（仅用于构建来源卡片）

    Wiki/GraphRAG 命中的回答上下文来自 wiki 页面/知识图谱，本身不携带文档 chunks，
    导致前端无来源卡片可展示。这里额外做一次快速 RAG 检索（不做 rerank，控制成本），
    用检索到的文档 chunks 支撑引用展示；检索失败或为空时不影响主回答。
    """
    try:
        from apps.retrieval.hybrid import hybrid_search
        result = hybrid_search(query, user, do_rerank=False,
                               node_ids=node_ids, root_types=root_types,
                               query_vector=query_vector)
        return result.get('chunks', [])[:limit]
    except Exception as e:
        logger.exception(f'[Router] 引用补充检索失败: {e}')
        return []


def decide_route(query: str, user, node_ids: Optional[List[int]] = None,
                 root_types: Optional[List[str]] = None) -> Dict:
    """三路并行路由决策。

    预计算 query embedding 后，Wiki / GraphRAG / RAG 三路同时执行，
    按置信度选择最佳结果（Wiki 优先 > GraphRAG > RAG 兜底）。

    并行后总延迟 = max(Wiki, GraphRAG, RAG)，而非三路之和，
    预期从 5-6s 降到 2-3s。

    Args:
        query: 用户问题
        user: 用户对象（透传给各检索层做权限过滤）
        node_ids: 可选，知识库节点范围限定（聊天页"知识库范围"选择，透传给 RAG 兜底层）
        root_types: 可选，根类型限定（透传给 RAG 兜底层）

    Returns:
        {
            'source': 'wiki' | 'graphrag_local' | 'graphrag_global' | 'rag',
            'context': str,
            'chunks': list, 'entities': list, 'relations': list,
            'communities': list, 'wiki_page': dict,
            'confidence': float,
            'route_trace': [{'layer','confidence','latency_ms'}],
            'latency_ms': int,
        }
    """
    from apps.graph.retriever import graphrag_search
    from apps.retrieval.hybrid import hybrid_search
    from apps.wiki.retriever import search_wiki
    from apps.llm.embedding import get_embedding_client, EmbeddingException

    t0 = time.time()
    route_trace = []

    # === 预计算 query 向量（三路复用，只调 1 次 Embedding API）===
    embed_client = get_embedding_client()
    try:
        qvec = embed_client.embed_one(query)
    except EmbeddingException as e:
        logger.error(f'[Router] query embedding failed: {e}，降级为 RAG BM25-only')
        qvec = None
    if qvec and all(v == 0.0 for v in qvec):
        qvec = None

    # === 三路并行提交 ===
    wiki_future = _route_pool.submit(_search_wiki, query, qvec, WIKI_SEARCH_THRESHOLD)
    graph_future = _route_pool.submit(_search_graph, query, user, qvec)
    rag_future = _route_pool.submit(_search_rag, query, user, node_ids, root_types, qvec)

    # === 收集结果 ===
    wiki_result = wiki_future.result()
    graph_result = graph_future.result()
    rag_result = rag_future.result()

    route_trace.append(wiki_result['trace'])
    route_trace.append(graph_result['trace'])
    route_trace.append(rag_result['trace'])

    # === 按置信度选择最佳结果（Wiki 优先 > GraphRAG > RAG 兜底）===
    wiki_confidence = wiki_result['confidence']
    graph_confidence = graph_result['confidence']
    rag_chunks = rag_result.get('chunks', [])
    rag_confidence = min(1.0, 0.3 + len(rag_chunks) * 0.05)

    # Wiki 命中（置信度最高，直接返回）
    if wiki_confidence >= WIKI_DIRECT_HIT_THRESHOLD:
        wiki_page = wiki_result['page']
        logger.info(f'[Router] Wiki 命中: {wiki_page["title"]} confidence={wiki_confidence:.2f}')
        return {
            'source': 'wiki',
            'context': f"# {wiki_page['title']}\n\n{wiki_page['content']}",
            'chunks': _citation_chunks(query, user, node_ids, root_types,
                                        query_vector=qvec),
            'entities': [],
            'relations': [],
            'communities': [],
            'wiki_page': wiki_page,
            'confidence': wiki_confidence,
            'route_trace': route_trace,
            'latency_ms': int((time.time() - t0) * 1000),
        }

    # GraphRAG 命中
    if graph_confidence >= GRAPH_CONFIDENCE_THRESHOLD:
        logger.info(f'[Router] GraphRAG 命中: {graph_result["source"]} confidence={graph_confidence:.2f}')
        return {
            'source': graph_result['source'],
            'context': graph_result['context'],
            'chunks': _citation_chunks(query, user, node_ids, root_types,
                                        query_vector=qvec),
            'entities': graph_result.get('entities', []),
            'relations': graph_result.get('relations', []),
            'communities': graph_result.get('communities', []),
            'wiki_page': None,
            'confidence': graph_confidence,
            'route_trace': route_trace,
            'latency_ms': int((time.time() - t0) * 1000),
        }

    # RAG 兜底
    rag_context = _format_rag_context(rag_chunks)
    logger.info(f'[Router] RAG 兜底 chunks={len(rag_chunks)} confidence={rag_confidence:.2f}')
    return {
        'source': 'rag',
        'context': rag_context,
        'chunks': rag_chunks,
        'entities': [],
        'relations': [],
        'communities': [],
        'wiki_page': None,
        'confidence': rag_confidence,
        'route_trace': route_trace,
        'latency_ms': int((time.time() - t0) * 1000),
    }


def _search_wiki(query: str, qvec, threshold: float) -> Dict:
    """Wiki 检索子任务（并行执行）"""
    from apps.wiki.retriever import search_wiki
    t = time.time()
    try:
        results = search_wiki(query, top_k=1, threshold=threshold,
                              query_vector=qvec)
    except Exception as e:
        logger.exception(f'[Router] Wiki 检索失败: {e}')
        results = []
    latency = int((time.time() - t) * 1000)
    confidence = results[0]['score'] if results else 0.0
    return {
        'confidence': confidence,
        'page': results[0] if results else None,
        'trace': {'layer': 'wiki', 'confidence': round(confidence, 4),
                  'latency_ms': latency},
    }


def _search_graph(query: str, user, qvec=None) -> Dict:
    """GraphRAG 检索子任务（并行执行，复用预计算向量）"""
    from apps.graph.retriever import graphrag_search
    t = time.time()
    try:
        result = graphrag_search(query, user, mode='auto', query_vector=qvec)
    except Exception as e:
        logger.exception(f'[Router] GraphRAG 检索失败: {e}')
        result = {'confidence': 0.0, 'source': 'graphrag', 'context': '',
                  'entities': [], 'relations': [], 'communities': []}
    latency = int((time.time() - t) * 1000)
    confidence = result.get('confidence', 0.0)
    return {
        'confidence': confidence,
        'source': result.get('source', 'graphrag'),
        'context': result.get('context', ''),
        'entities': result.get('entities', []),
        'relations': result.get('relations', []),
        'communities': result.get('communities', []),
        'trace': {'layer': 'graphrag', 'confidence': round(confidence, 4),
                  'latency_ms': latency},
    }


def _search_rag(query: str, user, node_ids, root_types, qvec) -> Dict:
    """RAG 检索子任务（并行执行，复用预计算向量）"""
    from apps.retrieval.hybrid import hybrid_search
    t = time.time()
    try:
        result = hybrid_search(query, user, do_rerank=True,
                               node_ids=node_ids, root_types=root_types,
                               query_vector=qvec)
    except Exception as e:
        logger.exception(f'[Router] RAG 兜底检索失败: {e}')
        result = {'chunks': [], 'stats': {}}
    latency = int((time.time() - t) * 1000)
    chunks = result.get('chunks', [])
    confidence = min(1.0, 0.3 + len(chunks) * 0.05)
    return {
        'chunks': chunks,
        'stats': result.get('stats', {}),
        'confidence': confidence,
        'trace': {'layer': 'rag', 'confidence': round(confidence, 4),
                  'latency_ms': latency},
    }


def orchestrate(query: str, user, session=None, node_ids: Optional[List[int]] = None,
                root_types: Optional[List[str]] = None) -> Dict:
    """编排入口：供 executor 或其他调用方使用。

    根据 SystemConfig 中 FAST_MODE_STRATEGY 配置选择检索策略：
    - parallel（默认）：三路并行检索（Wiki + GraphRAG + RAG），共享向量，置信度择优
    - sequential：串行降级（Wiki → GraphRAG → RAG），命中即停，省资源
    - rag_only：仅 RAG 混合检索，跳过 Wiki/GraphRAG，最省资源

    Args:
        query: 用户问题
        user: 用户对象
        session: 会话对象（预留）
        node_ids: 可选，知识库节点范围限定（透传给 RAG 兜底层）
        root_types: 可选，根类型限定（透传给 RAG 兜底层）

    Returns:
        同 decide_route
    """
    from apps.system.config_loader import get_config_value
    strategy = get_config_value('FAST_MODE_STRATEGY', default='rag_only', value_type='str')

    if strategy == 'parallel':
        return decide_route(query, user, node_ids=node_ids, root_types=root_types)
    elif strategy == 'sequential':
        return _decide_route_sequential(query, user, node_ids=node_ids, root_types=root_types)
    else:
        # rag_only：仅 RAG 混合检索
        return _decide_route_rag_only(query, user, node_ids=node_ids, root_types=root_types)


def _decide_route_sequential(query: str, user, node_ids=None, root_types=None) -> Dict:
    """串行降级路由决策（旧版行为，命中即停）。

    按 Wiki → GraphRAG → RAG 顺序尝试，返回第一个达到阈值的结果。
    比并行版省资源（不命中就不跑下一层），但延迟 = 三路之和。
    """
    from apps.graph.retriever import graphrag_search
    from apps.retrieval.hybrid import hybrid_search
    from apps.llm.embedding import get_embedding_client, EmbeddingException

    t0 = time.time()
    route_trace = []

    # 预计算 query 向量（Wiki 和 GraphRAG 复用）
    embed_client = get_embedding_client()
    try:
        qvec = embed_client.embed_one(query)
    except EmbeddingException as e:
        logger.error(f'[Router] query embedding failed: {e}')
        qvec = None
    if qvec and all(v == 0.0 for v in qvec):
        qvec = None

    # === 第 1 层：Wiki ===
    t1 = time.time()
    try:
        from apps.wiki.retriever import search_wiki
        wiki_results = search_wiki(query, top_k=1, threshold=WIKI_SEARCH_THRESHOLD,
                                   query_vector=qvec)
    except Exception as e:
        logger.exception(f'[Router] Wiki 检索失败: {e}')
        wiki_results = []
    wiki_latency = int((time.time() - t1) * 1000)
    wiki_confidence = wiki_results[0]['score'] if wiki_results else 0.0
    route_trace.append({'layer': 'wiki', 'confidence': round(wiki_confidence, 4),
                        'latency_ms': wiki_latency})

    if wiki_confidence >= WIKI_DIRECT_HIT_THRESHOLD:
        wiki_page = wiki_results[0]
        logger.info(f'[Router] Wiki 命中: {wiki_page["title"]} confidence={wiki_confidence:.2f}')
        return {
            'source': 'wiki',
            'context': f"# {wiki_page['title']}\n\n{wiki_page['content']}",
            'chunks': _citation_chunks(query, user, node_ids, root_types, query_vector=qvec),
            'entities': [], 'relations': [], 'communities': [],
            'wiki_page': wiki_page,
            'confidence': wiki_confidence,
            'route_trace': route_trace,
            'latency_ms': int((time.time() - t0) * 1000),
        }

    # === 第 2 层：GraphRAG ===
    t2 = time.time()
    try:
        graph_result = graphrag_search(query, user, mode='auto', query_vector=qvec)
    except Exception as e:
        logger.exception(f'[Router] GraphRAG 检索失败: {e}')
        graph_result = {'confidence': 0.0, 'source': 'graphrag', 'context': '',
                        'entities': [], 'relations': [], 'communities': []}
    graph_latency = int((time.time() - t2) * 1000)
    graph_confidence = graph_result.get('confidence', 0.0)
    route_trace.append({'layer': 'graphrag', 'confidence': round(graph_confidence, 4),
                        'latency_ms': graph_latency})

    if graph_confidence >= GRAPH_CONFIDENCE_THRESHOLD:
        logger.info(f'[Router] GraphRAG 命中: {graph_result["source"]} confidence={graph_confidence:.2f}')
        return {
            'source': graph_result['source'],
            'context': graph_result['context'],
            'chunks': _citation_chunks(query, user, node_ids, root_types, query_vector=qvec),
            'entities': graph_result.get('entities', []),
            'relations': graph_result.get('relations', []),
            'communities': graph_result.get('communities', []),
            'wiki_page': None,
            'confidence': graph_confidence,
            'route_trace': route_trace,
            'latency_ms': int((time.time() - t0) * 1000),
        }

    # === 第 3 层：RAG 兜底 ===
    t3 = time.time()
    try:
        rag_result = hybrid_search(query, user, do_rerank=True,
                                   node_ids=node_ids, root_types=root_types,
                                   query_vector=qvec)
    except Exception as e:
        logger.exception(f'[Router] RAG 兜底检索失败: {e}')
        rag_result = {'chunks': [], 'stats': {}}
    rag_latency = int((time.time() - t3) * 1000)
    rag_chunks = rag_result.get('chunks', [])
    rag_confidence = min(1.0, 0.3 + len(rag_chunks) * 0.05)
    route_trace.append({'layer': 'rag', 'confidence': round(rag_confidence, 4),
                        'latency_ms': rag_latency})
    rag_context = _format_rag_context(rag_chunks)
    logger.info(f'[Router] RAG 兜底 chunks={len(rag_chunks)} confidence={rag_confidence:.2f}')
    return {
        'source': 'rag',
        'context': rag_context,
        'chunks': rag_chunks,
        'entities': [], 'relations': [], 'communities': [],
        'wiki_page': None,
        'confidence': rag_confidence,
        'route_trace': route_trace,
        'latency_ms': int((time.time() - t0) * 1000),
    }


def _decide_route_rag_only(query: str, user, node_ids=None, root_types=None) -> Dict:
    """仅 RAG 混合检索（跳过 Wiki/GraphRAG，最省资源）。

    直接走基础混合检索（向量 + BM25 + RRF + Rerank），
    不调用 Embedding 做 Wiki/GraphRAG 检索，延迟最低。
    """
    from apps.retrieval.hybrid import _search_core

    t0 = time.time()
    route_trace = []

    try:
        t_rag = time.time()
        rag_result = _search_core(query, user, do_rerank=True,
                                  node_ids=node_ids, root_types=root_types)
        rag_latency = int((time.time() - t_rag) * 1000)
    except Exception as e:
        logger.exception(f'[Router] RAG 检索失败: {e}')
        rag_result = {'chunks': [], 'stats': {}}
        rag_latency = 0

    rag_chunks = rag_result.get('chunks', [])
    rag_confidence = min(1.0, 0.3 + len(rag_chunks) * 0.05)
    route_trace.append({'layer': 'rag', 'confidence': round(rag_confidence, 4),
                        'latency_ms': rag_latency})
    rag_context = _format_rag_context(rag_chunks)
    logger.info(f'[Router] RAG only chunks={len(rag_chunks)} confidence={rag_confidence:.2f}')
    return {
        'source': 'rag',
        'context': rag_context,
        'chunks': rag_chunks,
        'entities': [], 'relations': [], 'communities': [],
        'wiki_page': None,
        'confidence': rag_confidence,
        'route_trace': route_trace,
        'latency_ms': int((time.time() - t0) * 1000),
    }


def _format_rag_context(chunks: List[Dict]) -> str:
    """将 RAG chunks 格式化为上下文文本。

    每个 chunk 取前 500 字符，标注来源文档与章节路径。

    Args:
        chunks: hybrid_search 返回的 chunks 列表

    Returns:
        格式化上下文文本
    """
    if not chunks:
        return ''

    lines = []
    for i, c in enumerate(chunks[:5], 1):
        doc_title = c.get('doc_title', '未知文档')
        section = c.get('section_path', '')
        content = c.get('content', '')[:500]
        loc = f'（{section}）' if section else ''
        lines.append(f'[{i}] 来源：{doc_title}{loc}')
        lines.append(content)

    return '\n\n'.join(lines)
