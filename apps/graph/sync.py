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


def _graph_enabled() -> bool:
    """图谱抽取开关（SystemConfig.GRAPH_ENABLED，默认开启）

    配置关闭时文档解析完成后标记 skipped，不派发抽取任务。
    """
    from apps.system.config_loader import get_config_value
    return get_config_value('GRAPH_ENABLED', default=True, value_type='bool')


def _dispatch_node_graph_task(doc) -> bool:
    """节点级防抖派发图谱抽取任务

    文档完成/更新后调用：
    1. 配置关闭或无切片数据 → 标记该文档 graph_status=skipped，不派发
    2. 置文档 graph_status=pending（待构建）
    3. 原子 check-and-set 节点 graph_pending：仅首个文档完成者派发任务，
       同节点后续完成的文档合并到同一次任务中批量处理

    Args:
        doc: Document 实例（需含 node_id / chunk_count）

    Returns:
        True=已派发任务，False=跳过（配置关闭/无数据/已由他人派发）
    """
    from apps.knowledge.models import Document, KnowledgeNode

    if not _graph_enabled():
        Document.objects.filter(id=doc.id).update(graph_status='skipped')
        return False
    if not doc.node_id:
        return False
    # 无切片可抽取（如空文件解析成功），标记未启用，避免无意义的空抽取
    if doc.chunk_count == 0:
        Document.objects.filter(id=doc.id).update(graph_status='skipped')
        return False

    Document.objects.filter(id=doc.id).update(graph_status='pending')
    from apps.graph.tasks import graph_extract_task
    dispatched = KnowledgeNode.objects.filter(
        id=doc.node_id, is_deleted=False, graph_pending=False
    ).update(graph_pending=True)
    if dispatched:
        graph_extract_task.delay(doc.node_id)
        return True
    return False


def on_document_done(document_id: int):
    """文档解析完成后的增量同步入口：防抖派发节点级图谱抽取

    graph_extract_task 内部会先调用 _clean_graph_data 清理各文档旧数据，
    再执行抽取，保证重复解析场景数据一致性。

    Args:
        document_id: 文档 ID
    """
    from apps.knowledge.models import Document

    doc = Document.objects.filter(id=document_id).only('id', 'node_id', 'chunk_count').first()
    if doc is None:
        return
    dispatched = _dispatch_node_graph_task(doc)
    if dispatched:
        logger.info(f'[Graph Sync] 文档 {document_id} 完成，触发节点 {doc.node_id} 图谱抽取')


def on_document_updated(document_id: int):
    """文档更新后的处理：防抖派发节点级图谱重新抽取（清理 + 抽取由任务内部完成）

    Args:
        document_id: 文档 ID
    """
    from apps.knowledge.models import Document

    doc = Document.objects.filter(id=document_id).only('id', 'node_id', 'chunk_count').first()
    if doc is None:
        return
    dispatched = _dispatch_node_graph_task(doc)
    if dispatched:
        logger.info(f'[Graph Sync] 文档 {document_id} 更新，触发节点 {doc.node_id} 图谱重新抽取')


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


def on_document_deleted(document_id: int):
    """文档删除后的处理：清理图谱数据并标记社区待刷新

    仅清理该文档产生的数据，不触发重新抽取。

    Args:
        document_id: 文档 ID
    """
    from apps.graph.models import GraphCommunity

    # 文档删除可能发生在节点任务派发后尚未执行期间：清除该节点待处理标记，
    # 避免残留 True 导致该节点后续文档完成时不再派发（自愈）
    from apps.knowledge.models import Document, KnowledgeNode
    node_id = Document.objects.filter(id=document_id).values_list('node_id', flat=True).first()
    if node_id:
        KnowledgeNode.objects.filter(id=node_id).update(graph_pending=False)

    stats = _clean_graph_data(document_id)
    # 图谱结构变化，标记社区待刷新（下次社区检测会重建）
    GraphCommunity.objects.all().update(updated_at=timezone.now())
    logger.info(
        f'[Graph Sync] 文档 {document_id} 删除，清理关系 {stats["relations"]} 条，'
        f'删除实体 {stats["entities_deleted"]} 个，更新实体 {stats["entities_kept"]} 个')
