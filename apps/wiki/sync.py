"""Wiki 增量同步与数据一致性

文档变化联动 Wiki 页面：
- on_document_done_for_wiki：文档解析完成后，将该节点下已发布的 Wiki 页面
  标记为过期（status='expired'），由定时任务 refresh_expired_wiki_pages 基于
  最新文档内容重新生成，避免 Wiki 内容与文档内容长期不一致。
"""
from loguru import logger


def on_document_done_for_wiki(document_id: int):
    """文档完成后，标记关联节点 Wiki 为过期

    Args:
        document_id: 文档 ID
    """
    from apps.knowledge.models import Document
    from apps.wiki.models import WikiPage

    try:
        doc = Document.objects.get(id=document_id)
    except Document.DoesNotExist:
        return

    if not doc.node_id:
        return

    updated = WikiPage.objects.filter(
        node_id=doc.node_id, status='published'
    ).update(status='expired')
    if updated:
        logger.info(
            f'[Wiki Sync] 文档 {document_id} 完成，'
            f'标记节点 {doc.node_id} 的 {updated} 个 Wiki 页面为过期')
