"""图谱增量同步与数据一致性

文档生命周期变化时联动图谱数据：
- on_document_done：解析完成触发图谱抽取（抽取任务内部会先清理该文档旧数据）
- on_document_updated：文档更新（重新解析/版本升级）后触发重新抽取
- on_document_deleted：文档删除后清理该文档产生的图谱数据，并标记社区待刷新

数据一致性原则：
- 关系按 source_doc_ids 精确清理，避免残留过期边
- 实体从 source_doc_ids 中移除该文档；无其他来源时删除，有来源时保留并置空
  embedding，等待下次抽取时重新同步（_get_or_create_entity 以 embedding 为空
  判定为待同步）
- 清理逻辑在 graph_extract_task 内先于抽取执行，保证"重新解析"场景不会残留
  旧版本图谱数据
"""
from loguru import logger
from django.utils import timezone


def _clean_graph_data(document_id: int) -> dict:
    """清理某文档产生的图谱旧数据（关系 + 实体引用）

    在重新抽取前执行，或在文档删除时执行。

    Args:
        document_id: 文档 ID

    Returns:
        {'relations': 删除的关系数, 'entities_deleted': 删除的实体数,
         'entities_kept': 保留但移除来源的实体数, 'entities_to_refresh': 置空
         embedding 待刷新的实体数}
    """
    from apps.graph.models import GraphEntity, GraphRelation

    # 1. 删除该文档产生的旧关系（ArrayField contains 匹配 source_doc_ids 含该文档）
    deleted_relations, _ = GraphRelation.objects.filter(
        source_doc_ids__contains=[document_id]
    ).delete()

    # 2. 从实体的 source_doc_ids 中移除该文档
    #    实体还有其他来源 → 保留，置空 embedding 等待下次抽取重建向量
    #    实体无其他来源 → 直接删除（其关联关系随 CASCADE 一并清理）
    entities = GraphEntity.objects.filter(source_doc_ids__contains=[document_id])
    deleted_count = 0
    kept_count = 0
    refresh_ids = []
    for entity in entities:
        entity.source_doc_ids = [d for d in entity.source_doc_ids if d != document_id]
        if entity.source_doc_ids:
            # 保留实体：置空 embedding，下次抽取时按 embedding 为空重新同步向量
            entity.embedding = None
            entity.save(update_fields=['source_doc_ids', 'embedding'])
            kept_count += 1
            refresh_ids.append(entity.id)
        else:
            entity.delete()
            deleted_count += 1

    return {
        'relations': deleted_relations,
        'entities_deleted': deleted_count,
        'entities_kept': kept_count,
        'entities_to_refresh': len(refresh_ids),
    }


def on_document_done(document_id: int):
    """文档解析完成后的增量同步入口：触发图谱抽取

    graph_extract_task 内部会先调用 _clean_graph_data 清理该文档旧数据，
    再执行抽取，保证重复解析场景数据一致性。

    Args:
        document_id: 文档 ID
    """
    from apps.graph.tasks import graph_extract_task
    graph_extract_task.delay(document_id)
    logger.info(f'[Graph Sync] 文档 {document_id} 完成，触发图谱抽取')


def on_document_updated(document_id: int):
    """文档更新后的处理：触发重新抽取（清理 + 抽取由任务内部完成）

    Args:
        document_id: 文档 ID
    """
    from apps.graph.tasks import graph_extract_task
    graph_extract_task.delay(document_id)
    logger.info(f'[Graph Sync] 文档 {document_id} 更新，触发图谱重新抽取')


def on_document_deleted(document_id: int):
    """文档删除后的处理：清理图谱数据并标记社区待刷新

    仅清理该文档产生的数据，不触发重新抽取。

    Args:
        document_id: 文档 ID
    """
    from apps.graph.models import GraphCommunity

    stats = _clean_graph_data(document_id)
    # 图谱结构变化，标记社区待刷新（下次社区检测会重建）
    GraphCommunity.objects.all().update(updated_at=timezone.now())
    logger.info(
        f'[Graph Sync] 文档 {document_id} 删除，清理关系 {stats["relations"]} 条，'
        f'删除实体 {stats["entities_deleted"]} 个，更新实体 {stats["entities_kept"]} 个')
