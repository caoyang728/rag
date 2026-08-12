"""知识图谱 Celery 任务"""
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from loguru import logger

# 单次任务最多处理的文档数，避免切片过多时超时（每文档可能有数十上百个切片，
# 每个切片需一次 LLM 调用，总耗时不可预估；超时后 SoftTimeLimitExceeded 会中断循环，
# 剩余文档回退 pending 并由本任务末尾自动续派下一轮）
MAX_DOCS_PER_TASK = 5


@shared_task(queue='parse')
def graph_extract_task(node_id: int):
    """按节点防抖合并的图谱抽取任务：处理该节点下待抽取文档（分批）

    节点内多文档连续完成时合并为一次任务（防抖派发见 graph/sync.py）：
    1. 先清除节点待处理标记（graph_pending），任务崩溃后不残留，后续文档可重新触发
    2. 收集该节点所有 graph_status='pending' 的已完成文档
    3. 统一标记 extracting，避免并发任务重复处理同一批文档
    4. 逐文档清理旧图谱数据并抽取，结果回写 done/failed
    5. 单次任务最多处理 MAX_DOCS_PER_TASK 个文档，剩余文档由续派任务处理

    超时保护：捕获 SoftTimeLimitExceeded，将仍在 extracting 的文档回退 pending，
    然后续派新任务继续处理，避免文档卡在 extracting 状态。

    Args:
        node_id: 知识节点 ID
    """
    from apps.knowledge.models import Document, KnowledgeNode

    KnowledgeNode.objects.filter(id=node_id).update(graph_pending=False)
    from apps.graph.sync import _graph_enabled, _clean_graph_data
    if not _graph_enabled():
        Document.objects.filter(node_id=node_id, graph_status='pending').update(graph_status='skipped')
        return {'ok': True, 'processed': 0, 'skipped': True}

    # 取待处理文档，限制本批次最大数量
    docs = list(Document.objects.filter(
        node_id=node_id, is_deleted=False, status='done', graph_status='pending'
    )[:MAX_DOCS_PER_TASK])

    if not docs:
        return {'ok': True, 'processed': 0}

    # 仅标记本批次文档为 extracting，而非全部 pending 文档
    doc_ids = [d.id for d in docs]
    Document.objects.filter(id__in=doc_ids).update(graph_status='extracting')

    from apps.graph.extractor import batch_extract_for_document
    processed = 0
    failed = 0
    timed_out = False
    for doc in docs:
        try:
            _clean_graph_data(doc.id)
            batch_extract_for_document(doc.id)
            Document.objects.filter(id=doc.id).update(graph_status='done')
            processed += 1
        except SoftTimeLimitExceeded:
            # 超时：当前文档可能尚未完成，标记回 pending 等待续派任务重试
            timed_out = True
            Document.objects.filter(id=doc.id).update(graph_status='pending')
            logger.warning(f'[Graph Task] 节点 {node_id} 文档 {doc.id} 超时，回退 pending')
            break
        except Exception as e:
            failed += 1
            logger.exception(f'[Graph Task] 文档 {doc.id} 图谱抽取失败: {e}')
            Document.objects.filter(id=doc.id).update(graph_status='failed')

    # 超时中断后，将剩余已标记 extracting 但未处理的文档回退 pending
    if timed_out:
        remaining = Document.objects.filter(
            node_id=node_id, graph_status='extracting', is_deleted=False
        ).update(graph_status='pending')
        logger.info(f'[Graph Task] 节点 {node_id} 超时中断，{remaining} 个文档回退 pending')

    # 检查是否还有待处理文档，有则续派任务（避免依赖外部触发）
    has_more = Document.objects.filter(
        node_id=node_id, is_deleted=False, status='done', graph_status='pending'
    ).exists()
    if has_more:
        graph_extract_task.delay(node_id)
        logger.info(f'[Graph Task] 节点 {node_id} 还有待处理文档，续派新任务')

    logger.info(f'[Graph Task] 节点 {node_id} 图谱抽取完成: 成功 {processed}, 失败 {failed}, 共 {len(docs)}, 超时={timed_out}')
    return {'ok': True, 'processed': processed, 'failed': failed, 'total': len(docs), 'timed_out': timed_out}


@shared_task(queue='parse')
def community_detection_task():
    """定时或手动触发：社区检测 + 摘要生成"""
    from apps.graph.community import run_community_detection
    from apps.llm.factory import get_llm
    llm = get_llm()
    count = run_community_detection(llm)
    logger.info(f'[Graph Task] 社区检测完成，共 {count} 个社区')
    return count
