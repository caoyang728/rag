"""
pgvector 向量检索封装
- 使用 HNSW + cosine 距离
- SET LOCAL hnsw.ef_search=40 会话级调节精度
- 冗余权限字段 + root_type 一次 SQL 完成"向量相似度 + 权限过滤"
- pgvector.CosineDistance 表达式
"""
from loguru import logger
import time
from typing import List, Dict, Any, Optional

from django.conf import settings
from django.db import connection

from pgvector.django import CosineDistance
from apps.retrieval.models import DocumentVector
from apps.retrieval.permission import build_permission_q



def _apply_ef_search(cursor, ef: int):
    """设置本次连接的 HNSW ef_search 参数"""
    try:
        cursor.execute(f'SET LOCAL hnsw.ef_search = {int(ef)};')
    except Exception as e:
        logger.warning('[VectorStore] set hnsw.ef_search failed: %s', e)


def vector_search(query_vector: List[float],
                  user,
                  top_k: int = 30,
                  root_types: Optional[List[str]] = None,
                  node_path_prefix: Optional[str] = None,
                  node_ids: Optional[List[int]] = None,
                  ef_search: Optional[int] = None) -> List[Dict[str, Any]]:
    """向量检索
    返回：[{
        'chunk_id','document_id','node_id','content','score',
        'visibility','root_type','node_path','doc_title'(可空)
    }] 按 score 降序（1 - 距离）"""
    t0 = time.time()
    ef = ef_search or settings.HNSW_EF_SEARCH

    # ⭐ 权限过滤走 Django ORM 的 Q，避免手写 SQL
    q = build_permission_q(user, root_types=root_types, node_path_prefix=node_path_prefix, node_ids=node_ids)

    # 走一次 connection 保证 SET LOCAL 生效
    with connection.cursor() as cur:
        _apply_ef_search(cur, ef)

    qs = (
        DocumentVector.objects
        .filter(q)
        .annotate(distance=CosineDistance('embedding', query_vector))
        .order_by('distance')
        .values('id', 'chunk_id', 'document_id', 'node_id', 'visibility',
                'root_type', 'node_path', 'content_preview', 'chunk_type', 'distance')[:top_k]
    )
    results = []
    for row in qs:
        distance = float(row['distance'] or 0.0)
        score = max(0.0, 1.0 - distance)  # cosine: distance=1-cos_sim
        results.append({
            'vector_id': row['id'],
            'chunk_id': row['chunk_id'],
            'document_id': row['document_id'],
            'node_id': row['node_id'],
            'content': row['content_preview'],
            'chunk_type': row['chunk_type'],
            'visibility': row['visibility'],
            'root_type': row['root_type'],
            'node_path': row['node_path'],
            'score': score,
            'distance': distance,
        })
    logger.info('[VectorStore] hit=%d latency=%dms', len(results), int((time.time() - t0) * 1000))
    return results


def upsert_vector(chunk, embedding: List[float]) -> DocumentVector:
    """写入或更新一个 chunk 的向量
    从 chunk 反查冗余权限字段"""
    doc = chunk.document
    from apps.users.models import UserTeam
    owner_team_id = doc.owner_team_id
    if owner_team_id is None:
        team = UserTeam.objects.filter(user_id=doc.owner_id).first()
        owner_team_id = team.team_id if team else None

    # jieba 分词提取关键词
    keywords = _extract_keywords(chunk.content)

    vec, created = DocumentVector.objects.update_or_create(
        chunk_id=chunk.id,
        defaults={
            'document_id': doc.id,
            'embedding': embedding,
            'visibility': doc.visibility,
            'owner_id': doc.owner_id,
            'owner_team_id': owner_team_id,
            'root_type': doc.root_type,
            'node_id': doc.node_id,
            'node_path': getattr(doc.node, 'path', '/') if doc.node else '/',
            'chunk_type': chunk.chunk_type,
            'content_preview': (chunk.content or '')[:200],
            'keywords': keywords,
        }
    )
    return vec


def _extract_keywords(text: str, topk: int = 10) -> List[str]:
    """jieba 提取关键词"""
    if not text:
        return []
    try:
        import jieba.analyse
        return jieba.analyse.extract_tags(text, topK=topk)
    except Exception:
        return []


def delete_by_document(document_id: int):
    """删除指定文档的所有向量"""
    deleted = DocumentVector.objects.filter(document_id=document_id).delete()
    logger.info('[VectorStore] deleted %d vectors for document %d', deleted[0] if deleted else 0, document_id)
    return deleted
