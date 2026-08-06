"""
实体 Embedding 同步
- build_entity_embedding_text: 构建实体 embedding 文本（名称 + 类型 + 描述）
- sync_entity_embeddings: 批量同步实体向量，存入 pgvector
"""
from typing import List, Optional

from loguru import logger

from apps.graph.models import GraphEntity
from apps.llm.embedding import get_embedding_client


def build_entity_embedding_text(entity: GraphEntity) -> str:
    """构建实体 embedding 文本。

    格式："{name} 是{type_label}。{description}"
    与检索 query 的语义对齐：query 也按"实体名/类型/描述"语义召回。

    Args:
        entity: 图谱实体

    Returns:
        embedding 输入文本
    """
    type_map = dict(GraphEntity.TYPE_CHOICES)
    type_label = type_map.get(entity.type, entity.type)
    return f"{entity.name} 是{type_label}。{entity.description}"


def sync_entity_embeddings(entity_ids: List[int] = None, batch_size: int = 32) -> int:
    """批量同步实体 embedding。

    - 指定 entity_ids 时，仅处理其中 embedding 为空的实体（避免重复计算已有向量的实体）
    - entity_ids 为 None 时，同步所有 embedding 为空的实体
    - 向量全零（embedding 服务异常/空返回）的实体跳过写入

    Args:
        entity_ids: 指定实体 ID 列表；None 表示同步全部缺失向量
        batch_size: 每批处理数量

    Returns:
        成功写入向量的实体数量
    """
    client = get_embedding_client()

    qs = GraphEntity.objects.all()
    if entity_ids is not None:
        qs = qs.filter(id__in=entity_ids)
    qs = qs.filter(embedding__isnull=True)

    entities = list(qs)
    updated_count = 0

    for i in range(0, len(entities), batch_size):
        batch = entities[i:i + batch_size]
        texts = [build_entity_embedding_text(e) for e in batch]
        try:
            vectors = client.embed(texts)
        except Exception as e:
            logger.error(f'[Graph Embedding] batch embed failed: {e}')
            continue

        to_update = []
        for entity, vec in zip(batch, vectors):
            if vec and not all(v == 0.0 for v in vec):
                entity.embedding = vec
                updated_count += 1
                to_update.append(entity)

        if to_update:
            GraphEntity.objects.bulk_update(to_update, fields=['embedding'])

    logger.info(f'[Graph Embedding] synced {updated_count}/{len(entities)} entities')
    return updated_count
