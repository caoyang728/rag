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
    """设置本次连接的 HNSW ef_search 参数

    使用参数化查询避免 SQL 注入：ef 来自 settings/调用方传入，虽已 int() 钳制，
    但仍统一走占位符，避免动态拼接 SQL 字符串。
    """
    try:
        # 参数化查询：ef 已在外层 int() 钳制为整数，此处用占位符防止 SQL 注入
        cursor.execute('SET LOCAL hnsw.ef_search = %s;', (int(ef),))
    except Exception as e:
        logger.warning(f'[VectorStore] set hnsw.ef_search failed: {e}')


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
        'visibility_level','root_type','node_path','doc_title'(可空)
    }] 按 score 降序（1 - 距离）"""
    t0 = time.time()
    ef = ef_search or settings.HNSW_EF_SEARCH

    q = build_permission_q(user, root_types=root_types, node_path_prefix=node_path_prefix, node_ids=node_ids)

    with connection.cursor() as cur:
        _apply_ef_search(cur, ef)

    qs = (
        DocumentVector.objects
        .filter(q)
        .annotate(distance=CosineDistance('embedding', query_vector))
        .order_by('distance')
        .values('id', 'chunk_id', 'document_id', 'node_id', 'visibility_level',
                'root_type', 'node_path', 'content_preview', 'chunk_type', 'distance')[:top_k]
    )
    results = []
    for row in qs:
        distance = float(row['distance'] or 0.0)
        score = max(0.0, 1.0 - distance)
        results.append({
            'vector_id': row['id'],
            'chunk_id': row['chunk_id'],
            'document_id': row['document_id'],
            'node_id': row['node_id'],
            'content': row['content_preview'],
            'chunk_type': row['chunk_type'],
            'visibility_level': row['visibility_level'],
            'root_type': row['root_type'],
            'node_path': row['node_path'],
            'score': score,
            'distance': distance,
        })
    logger.info(f'[VectorStore] hit={len(results)} latency={int((time.time() - t0) * 1000)}ms')
    return results


def upsert_vector(chunk, embedding: List[float]) -> DocumentVector:
    """写入或更新一个 chunk 的向量

    从 chunk.document 同步冗余权限字段到 DocumentVector，保证检索时无需 JOIN document 表。
    字段对齐：visibility_level / dept_id / team_id / owner_id / node_id / node_path /
              has_resource_share / has_block_user / root_type
    """
    doc = chunk.document
    # 团队/部门归属直接从 Document 冗余字段读取（写入文档时已同步）
    team_id = doc.team_id
    dept_id = doc.dept_id
    # 节点 path 从关联节点读取（用于节点级共享前缀匹配）
    node_path = getattr(doc.node, 'path', '/') if doc.node else '/'

    keywords = _extract_keywords(chunk.content)

    vec, created = DocumentVector.objects.update_or_create(
        chunk_id=chunk.id,
        defaults={
            'document_id': doc.id,
            'embedding': embedding,
            'visibility_level': doc.visibility_level,
            'dept_id': dept_id,
            'team_id': team_id,
            'owner_id': doc.owner_id,
            'root_type': doc.root_type,
            'node_id': doc.node_id,
            'node_path': node_path,
            'has_resource_share': doc.has_resource_share,
            'has_block_user': doc.has_block_user,
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
    logger.info(f'[VectorStore] deleted {deleted[0] if deleted else 0} vectors for document {document_id}')
    return deleted
