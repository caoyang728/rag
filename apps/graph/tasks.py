"""知识图谱 Celery 任务"""
from celery import shared_task
from loguru import logger


@shared_task(queue='parse')
def graph_extract_task(document_id: int):
    """文档解析完成后触发图谱抽取

    抽取前先清理该文档旧图谱数据（关系 + 实体引用），保证重复解析/版本升级
    场景下不会残留旧版本数据（增量同步一致性）。

    Args:
        document_id: 文档 ID
    """
    from apps.graph.sync import _clean_graph_data
    stats = _clean_graph_data(document_id)
    from apps.graph.extractor import batch_extract_for_document
    batch_extract_for_document(document_id)
    logger.info(
        f'[Graph Task] 文档 {document_id} 图谱抽取完成 '
        f'(清理关系 {stats["relations"]} 条，删除实体 {stats["entities_deleted"]} 个)')


@shared_task(queue='parse')
def community_detection_task():
    """定时或手动触发：社区检测 + 摘要生成"""
    from apps.graph.community import run_community_detection
    from apps.llm.factory import get_llm
    llm = get_llm()
    count = run_community_detection(llm)
    logger.info(f'[Graph Task] 社区检测完成，共 {count} 个社区')
    return count
