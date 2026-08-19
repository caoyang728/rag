"""
Wiki 页面向量检索
"""
from typing import List, Dict, Optional

from loguru import logger
from pgvector.django import CosineDistance

from apps.llm.embedding import get_embedding_client
from apps.wiki.models import WikiPage


def search_wiki(query: str, top_k: int = 3, threshold: float = 0.78,
                query_vector: Optional[List[float]] = None) -> List[Dict]:
    """检索 Wiki 页面。

    Args:
        query: 用户问题
        top_k: 返回数量
        threshold: 最低相似度阈值，低于此值不返回
        query_vector: 可选，预计算的 query embedding 向量；传入时跳过 Embedding 调用

    Returns:
        [{'wiki_id', 'title', 'summary', 'content', 'tags', 'score'}]
        按 score 降序
    """
    if query_vector is not None:
        qvec = query_vector
    else:
        embed_client = get_embedding_client()
        qvec = embed_client.embed_one(query)

    if all(v == 0.0 for v in qvec):
        return []

    qs = (WikiPage.objects
          .filter(embedding__isnull=False, status='published')
          .annotate(distance=CosineDistance('embedding', qvec))
          .order_by('distance')
          .values('id', 'title', 'summary', 'content', 'tags', 'distance')[:top_k * 2])

    results = []
    for row in qs:
        score = max(0.0, 1.0 - float(row['distance'] or 0.0))
        if score >= threshold:
            results.append({
                'wiki_id': row['id'],
                'title': row['title'],
                'summary': row['summary'],
                'content': row['content'],
                'tags': row['tags'],
                'score': round(score, 4),
            })

    return results[:top_k]
