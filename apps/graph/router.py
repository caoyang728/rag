"""
三层路由 Orchestrator（LLM Wiki → GraphRAG → RAG 兜底）
- decide_route: 按 Wiki → GraphRAG → RAG 顺序尝试，返回第一个达到阈值的结果
- orchestrate: 编排入口（供 executor 等调用方使用）
"""
import time
from typing import Dict, List, Optional

from loguru import logger

# 可配置阈值（后续可迁移到 DB 配置）
# 说明：BGE-M3 的 cosine 相似度分布下，完全一致的标题约 0.75、强相关问题约 0.70，
# 因此 Wiki 直接命中阈值取 0.68，检索参与阈值取 0.55（低于此值视为 Wiki 无关）。
WIKI_DIRECT_HIT_THRESHOLD = 0.68   # Wiki 直接命中：高于此值直接返回
WIKI_SEARCH_THRESHOLD = 0.55       # Wiki 检索最低阈值
GRAPH_CONFIDENCE_THRESHOLD = 0.45  # GraphRAG 接受阈值（实体全相关+有关系的场景约 0.5+）


def decide_route(query: str, user) -> Dict:
    """三层路由决策。

    按 Wiki → GraphRAG → RAG 顺序尝试，返回第一个达到阈值的结果。
    路由链路的每层置信度与耗时都会记录到 route_trace，供评估与监控使用。

    Args:
        query: 用户问题
        user: 用户对象（透传给各检索层做权限过滤）

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

    t0 = time.time()
    route_trace = []

    # === 第 1 层：Wiki 快速命中 ===
    t1 = time.time()
    wiki_results = search_wiki(query, top_k=1, threshold=WIKI_SEARCH_THRESHOLD)
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
            'chunks': [],
            'entities': [],
            'relations': [],
            'communities': [],
            'wiki_page': wiki_page,
            'confidence': wiki_confidence,
            'route_trace': route_trace,
            'latency_ms': int((time.time() - t0) * 1000),
        }

    # === 第 2 层：GraphRAG 检索 ===
    t2 = time.time()
    graph_result = graphrag_search(query, user, mode='auto')
    graph_latency = int((time.time() - t2) * 1000)
    graph_confidence = graph_result.get('confidence', 0.0)
    route_trace.append({'layer': 'graphrag', 'confidence': round(graph_confidence, 4),
                        'latency_ms': graph_latency})

    if graph_confidence >= GRAPH_CONFIDENCE_THRESHOLD:
        logger.info(f'[Router] GraphRAG 命中: {graph_result.get("source")} confidence={graph_confidence:.2f}')
        return {
            'source': graph_result.get('source', 'graphrag'),
            'context': graph_result.get('context', ''),
            'chunks': [],
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
        rag_result = hybrid_search(query, user, do_rerank=True)
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
        'entities': [],
        'relations': [],
        'communities': [],
        'wiki_page': None,
        'confidence': rag_confidence,
        'route_trace': route_trace,
        'latency_ms': int((time.time() - t0) * 1000),
    }


def orchestrate(query: str, user, session=None) -> Dict:
    """编排入口：供 executor 或其他调用方使用。

    目前直接调用 decide_route，后续可扩展会话级缓存等。

    Args:
        query: 用户问题
        user: 用户对象
        session: 会话对象（预留）

    Returns:
        同 decide_route
    """
    return decide_route(query, user)


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
