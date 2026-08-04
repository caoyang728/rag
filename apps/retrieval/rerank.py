"""
Rerank - BGE-reranker-v2-m3 精排
交叉编码器（cross-encoder）比双塔向量更精准，用于最后一层精排
走 SiliconFlow /v1/rerank API
"""
from loguru import logger
from typing import List, Dict, Any

from apps.llm.embedding import get_embedding_client



def rerank_docs(query: str, docs: List[Dict[str, Any]], top_k: int = 5) -> List[Dict[str, Any]]:
    """接收混合召回后的 docs，返回 rerank 后的 top_k"""
    if not docs:
        return []
    client = get_embedding_client()
    texts = [d.get('content', '') or '' for d in docs]
    hits = client.rerank(query, texts, top_k=top_k)
    if not hits:
        return docs[:top_k]

    result = []
    for hit in hits:
        idx = hit['index']
        if 0 <= idx < len(docs):
            doc = dict(docs[idx])
            doc['rerank_score'] = float(hit.get('score', 0.0))
            result.append(doc)
    logger.info(f'[Rerank] returned={len(result)}')
    return result
