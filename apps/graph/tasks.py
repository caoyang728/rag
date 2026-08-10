"""知识图谱 Celery 任务"""
from celery import shared_task
from loguru import logger


@shared_task(queue='parse')
def graph_extract_task(node_id: int):
    """按节点防抖合并的图谱抽取任务：处理该节点下所有待抽取文档

    节点内多文档连续完成时合并为一次任务（防抖派发见 graph/sync.py）：
    1. 先清除节点待处理标记（graph_pending），任务崩溃后不残留，后续文档可重新触发
    2. 收集该节点所有 graph_status='pending' 的已完成文档
    3. 统一标记 extracting，避免并发任务重复处理同一批文档
    4. 逐文档清理旧图谱数据并抽取，结果回写 done/failed

    Args:
        node_id: 知识节点 ID
    """
    from apps.knowledge.models import Document, KnowledgeNode

    KnowledgeNode.objects.filter(id=node_id).update(graph_pending=False)
    from apps.graph.sync import _graph_enabled, _clean_graph_data
    if not _graph_enabled():
        Document.objects.filter(node_id=node_id, graph_status='pending').update(graph_status='skipped')
        return {'ok': True, 'processed': 0, 'skipped': True}

    docs = list(Document.objects.filter(
        node_id=node_id, is_deleted=False, status='done', graph_status='pending'))

    if not docs:
        return {'ok': True, 'processed': 0}

    # 统一标记 extracting，防止并发任务重复处理同一批文档
    Document.objects.filter(id__in=[d.id for d in docs]).update(graph_status='extracting')

    from apps.graph.extractor import batch_extract_for_document
    processed = 0
    failed = 0
    for doc in docs:
        try:
            _clean_graph_data(doc.id)
            batch_extract_for_document(doc.id)
            Document.objects.filter(id=doc.id).update(graph_status='done')
            processed += 1
        except Exception as e:
            failed += 1
            logger.exception(f'[Graph Task] 文档 {doc.id} 图谱抽取失败: {e}')
            Document.objects.filter(id=doc.id).update(graph_status='failed')

    logger.info(f'[Graph Task] 节点 {node_id} 图谱抽取完成: 成功 {processed}, 失败 {failed}, 共 {len(docs)}')
    return {'ok': True, 'processed': processed, 'failed': failed, 'total': len(docs)}


@shared_task(queue='parse')
def community_detection_task():
    """定时或手动触发：社区检测 + 摘要生成"""
    from apps.graph.community import run_community_detection
    from apps.llm.factory import get_llm
    llm = get_llm()
    count = run_community_detection(llm)
    logger.info(f'[Graph Task] 社区检测完成，共 {count} 个社区')
    return count
