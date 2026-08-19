"""
GraphRAG 检索器
- local_search: 局部检索 —— 向量命中实体后做多跳关系扩展，聚合实体+关系上下文
- global_search: 全局检索 —— 命中实体所属社区，返回社区摘要+关键实体
- graphrag_search: 统一入口（auto 先 local 后 global，按置信度择优）
"""
import time
from typing import Dict, List, Optional

from django.db.models import Q
from loguru import logger

from apps.graph.models import GraphEntity, GraphRelation, GraphCommunity
from apps.graph.vector_search import search_entities
from apps.llm.embedding import get_embedding_client

# 实体匹配最低分：低于此值认为图谱中无相关内容，返回空结果
ENTITY_MATCH_THRESHOLD = 0.3
# 全局检索实体匹配门槛：global 输出的是社区摘要（粗粒度内容），对实体相似度要求更高。
# 实测无关查询最高实体分仅约 0.36，而强相关查询在 0.5+，取 0.4 可清晰区分"无关/相关"。
GLOBAL_ENTITY_GATE = 0.4


def local_search(query: str, user, max_hops: int = 2, top_k_entities: int = 5,
                 query_vector: Optional[List[float]] = None) -> Dict:
    """局部图谱检索（Local Search）。

    流程：
    1. 生成 query 向量，检索语义相似的实体
    2. 从高分实体出发，做 max_hops 跳关系扩展
    3. 聚合实体描述 + 关系路径 → 构建上下文

    Args:
        query: 用户问题
        user: 用户对象（预留权限过滤）
        max_hops: 关系扩展最大跳数
        top_k_entities: 向量检索实体数
        query_vector: 可选，预计算的 query embedding 向量；传入时跳过 Embedding 调用

    Returns:
        {
            'context': str,
            'entities': [{'id','name','type','description'}],
            'relations': [{'source_name','target_name','type'}],
            'confidence': float,
            'source': 'graphrag_local',
        }
    """
    t0 = time.time()

    # 1. 使用预计算向量或生成 query 向量，检索相关实体
    if query_vector is not None:
        qvec = query_vector
    else:
        embed_client = get_embedding_client()
        qvec = embed_client.embed_one(query)

    if all(v == 0.0 for v in qvec):
        return _empty_result('graphrag_local')

    entity_hits = search_entities(qvec, top_k=top_k_entities)

    if not entity_hits:
        return _empty_result('graphrag_local')

    # 无实体明显相关时返回空：向量检索总是返回最近邻，最高分过低说明图谱中
    # 没有与 query 相关的内容，避免无关查询返回噪声上下文干扰 RAG 兜底
    if entity_hits[0]['score'] < ENTITY_MATCH_THRESHOLD:
        return _empty_result('graphrag_local')

    # 2. 取高分实体（score > 0.5）作为种子实体，否则取前 3 个
    seed_entity_ids = [e['entity_id'] for e in entity_hits if e['score'] > 0.5]
    if not seed_entity_ids:
        seed_entity_ids = [e['entity_id'] for e in entity_hits[:3]]

    # 3. 多跳关系扩展（无向扩展：source 或 target 命中即纳入邻居）
    related_entity_ids = set(seed_entity_ids)
    current_ids = set(seed_entity_ids)

    for _ in range(max_hops):
        if not current_ids:
            break

        relations = GraphRelation.objects.filter(
            Q(source_entity_id__in=current_ids) | Q(target_entity_id__in=current_ids)
        ).select_related('source_entity', 'target_entity')

        next_ids = set()
        for rel in relations:
            next_ids.add(rel.source_entity_id)
            next_ids.add(rel.target_entity_id)

        current_ids = next_ids - related_entity_ids
        related_entity_ids.update(current_ids)

    # 4. 获取所有关联实体和关系
    entities = GraphEntity.objects.filter(id__in=related_entity_ids)
    all_relations = GraphRelation.objects.filter(
        Q(source_entity_id__in=related_entity_ids) & Q(target_entity_id__in=related_entity_ids)
    ).select_related('source_entity', 'target_entity')

    # 5. 构建上下文
    context_parts = []

    context_parts.append("【相关实体】")
    for e in entities:
        context_parts.append(f"- {e.name}（{e.get_type_display()}）：{e.description[:200]}")

    context_parts.append("\n【实体关系】")
    for r in all_relations:
        context_parts.append(f"- {r.source_entity.name} --[{r.relation_type}]--> {r.target_entity.name}")

    # 6. 计算置信度：种子实体相似度 * 0.6 + 关系覆盖度 * 0.4
    avg_score = sum(
        e['score'] for e in entity_hits
        if e['entity_id'] in seed_entity_ids
    ) / max(len(seed_entity_ids), 1)
    relation_count = all_relations.count()
    confidence = min(1.0, avg_score * 0.6 + min(relation_count / 10, 1.0) * 0.4)

    latency = int((time.time() - t0) * 1000)
    logger.info(
        f'[GraphRAG] local_search entities={len(entities)} relations={relation_count} '
        f'confidence={confidence:.2f} latency={latency}ms')

    return {
        'context': '\n'.join(context_parts),
        'entities': [{'id': e.id, 'name': e.name, 'type': e.type,
                      'description': e.description[:100]} for e in entities],
        'relations': [{'source_name': r.source_entity.name,
                       'target_name': r.target_entity.name,
                       'type': r.relation_type} for r in all_relations],
        'confidence': round(confidence, 2),
        'source': 'graphrag_local',
        'latency_ms': latency,
    }


def global_search(query: str, user, top_k_communities: int = 3) -> Dict:
    """全局图谱检索（Global Search）。

    流程：
    1. 生成 query 向量，检索语义相似的实体
    2. 找实体所属的细粒度社区（level=0）
    3. 返回最相关社区摘要 + 社区内关键实体

    Args:
        query: 用户问题
        user: 用户对象（预留权限过滤）
        top_k_communities: 返回社区数

    Returns:
        {
            'context': str,
            'communities': [{'community_id','summary','keywords','level','topic'}],
            'confidence': float,
            'source': 'graphrag_global',
        }
    """
    t0 = time.time()

    embed_client = get_embedding_client()
    qvec = embed_client.embed_one(query)

    if all(v == 0.0 for v in qvec):
        return _empty_result('graphrag_global')

    # 检索实体
    entity_hits = search_entities(qvec, top_k=20)
    if not entity_hits:
        return _empty_result('graphrag_global')

    # 实体匹配低于全局门槛时直接返回空：向量检索总是返回最近邻，无关查询（如
    # "今天食堂有什么菜"最高实体分仅约 0.36）也会命中若干社区；若仅按社区数量给
    # 置信度会误判高置信。global 面向粗粒度社区主题，必须要求实体相似度足够强。
    top_score = entity_hits[0]['score']
    if top_score < GLOBAL_ENTITY_GATE:
        logger.info(f'[GraphRAG] global_search 实体匹配过低 top_score={top_score:.3f}，判定无关')
        return _empty_result('graphrag_global')

    # 找实体所属社区（level=0 细粒度社区）
    entity_ids = [e['entity_id'] for e in entity_hits]
    communities = GraphCommunity.objects.filter(
        entity_ids__overlap=entity_ids, level=0
    ).order_by('-updated_at')[:top_k_communities]

    if not communities:
        return _empty_result('graphrag_global')

    # 构建上下文
    context_parts = []
    context_parts.append("【相关知识领域】")

    for c in communities:
        comm_entities = GraphEntity.objects.filter(id__in=c.entity_ids)
        entity_names = [e.name for e in comm_entities]

        context_parts.append(f"\n领域：{c.metadata.get('topic', '未命名')}")
        context_parts.append(f"摘要：{c.summary[:300]}")
        context_parts.append(f"关键词：{', '.join(c.keywords[:5])}")
        context_parts.append(f"主要实体：{', '.join(entity_names[:8])}")

    # 置信度 = 实体相似度 × 0.85 + 社区覆盖度 × 0.15
    # 实体相似度才是图谱相关性的真实信号，社区数量只作轻微加成：
    # 无关查询即使命中多个社区（社区命中几乎必然发生），置信度也抬不上去。
    community_coverage = min(1.0, len(communities) / top_k_communities)
    confidence = min(1.0, top_score * 0.85 + community_coverage * 0.15)

    latency = int((time.time() - t0) * 1000)
    logger.info(
        f'[GraphRAG] global_search communities={len(communities)} top_score={top_score:.3f} '
        f'confidence={confidence:.2f} latency={latency}ms')

    return {
        'context': '\n'.join(context_parts),
        'communities': [{
            'community_id': c.community_id,
            'summary': c.summary[:200],
            'keywords': c.keywords[:5],
            'level': c.level,
            'topic': c.metadata.get('topic', ''),
        } for c in communities],
        'confidence': round(confidence, 2),
        'source': 'graphrag_global',
        'latency_ms': latency,
    }


def graphrag_search(query: str, user, mode: str = 'auto',
                    query_vector: Optional[List[float]] = None) -> Dict:
    """GraphRAG 检索统一入口。

    Args:
        query: 用户问题
        user: 用户对象
        mode: 'auto'（先 local 再 global，按置信度择优）/ 'local' / 'global'
        query_vector: 可选，预计算的 query embedding 向量；传入时跳过 Embedding 调用

    Returns:
        统一格式的检索结果
    """
    if mode == 'global':
        result = global_search(query, user)
    elif mode == 'local':
        result = local_search(query, user, query_vector=query_vector)
    else:
        # auto: 先尝试 local，置信度不足则尝试 global 并择优
        result = local_search(query, user, query_vector=query_vector)
        if result['confidence'] < 0.3:
            global_result = global_search(query, user)
            if global_result['confidence'] > result['confidence']:
                result = global_result

    return result


def _empty_result(source: str) -> Dict:
    """空结果统一格式（各检索器共用）"""
    return {
        'context': '',
        'entities': [],
        'relations': [],
        'communities': [],
        'confidence': 0.0,
        'source': source,
        'latency_ms': 0,
    }
