"""
图谱实体向量检索
- search_entities: 按向量检索语义相似的实体
- search_entities_by_name: 按名称检索实体（实体匹配阶段用）
"""
from typing import List, Dict, Optional

from pgvector.django import CosineDistance

from apps.graph.models import GraphEntity


def search_entities(query_vector: List[float], top_k: int = 10,
                    entity_types: Optional[List[str]] = None) -> List[Dict]:
    """按向量检索语义相似的实体。

    Args:
        query_vector: query 的 embedding 向量
        top_k: 返回数量
        entity_types: 可选，按实体类型过滤

    Returns:
        [{'entity_id', 'name', 'type', 'description', 'score'}]
        按 score 降序（score = 1 - cosine_distance）
    """
    qs = GraphEntity.objects.filter(embedding__isnull=False)
    if entity_types:
        qs = qs.filter(type__in=entity_types)

    qs = (qs
          .annotate(distance=CosineDistance('embedding', query_vector))
          .order_by('distance')
          .values('id', 'name', 'type', 'description', 'distance')[:top_k])

    results = []
    for row in qs:
        distance = float(row['distance'] or 0.0)
        score = max(0.0, 1.0 - distance)
        results.append({
            'entity_id': row['id'],
            'name': row['name'],
            'type': row['type'],
            'description': row['description'],
            'score': round(score, 4),
        })

    return results


def search_entities_by_name(name: str, exact: bool = False) -> List[GraphEntity]:
    """按名称检索实体（用于实体匹配阶段）。

    Args:
        name: 实体名称
        exact: True=精确匹配(大小写不敏感), False=模糊匹配(包含)

    Returns:
        匹配的实体对象列表
    """
    if not name or not name.strip():
        return []
    if exact:
        return list(GraphEntity.objects.filter(name__iexact=name.strip()))
    return list(GraphEntity.objects.filter(name__icontains=name.strip()))
