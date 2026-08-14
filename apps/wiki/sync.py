"""Wiki 增量同步与数据一致性

文档变化联动 Wiki 页面：
- on_document_done_for_wiki：文档解析完成后
  1. 将该节点下已发布的 Wiki 页面标记为过期（status='expired'），
     保证 Wiki 内容与文档内容长期一致
  2. 置文档 wiki_status=pending 并按节点防抖派发构建任务
     （build_node_wiki_task 基于最新文档内容重新生成页面）
- 防抖合并：同节点多文档连续完成只派发一次构建任务，批量处理后统一回写状态，
  避免每次文档上传都触发一次全量 LLM 重建
"""
from loguru import logger


def _wiki_enabled() -> bool:
    """Wiki 生成开关（SystemConfig.WIKI_ENABLED，默认开启）

    配置关闭时文档解析完成后标记 skipped，不派发构建任务。
    """
    from apps.system.config_loader import get_config_value
    return get_config_value('WIKI_ENABLED', default=True, value_type='bool')


def on_document_done_for_wiki(document_id: int):
    """文档完成后触发节点 Wiki 构建（防抖派发）

    Args:
        document_id: 文档 ID
    """
    from apps.knowledge.models import Document, KnowledgeNode
    from apps.wiki.models import WikiPage

    doc = Document.objects.filter(id=document_id).only(
        'id', 'node_id', 'audit_status'
    ).first()
    if not doc or not doc.node_id:
        return
    # 已驳回文档不再触发 Wiki 构建（驳回即终态，数据已清理）
    if doc.audit_status == 'rejected':
        return

    # 配置关闭：标记未启用，不派发
    if not _wiki_enabled():
        Document.objects.filter(id=document_id).update(wiki_status='skipped')
        return

    # 将节点下已发布 Wiki 页面标记为过期，等待重建
    updated = WikiPage.objects.filter(
        node_id=doc.node_id, status='published'
    ).update(status='expired')
    if updated:
        logger.info(
            f'[Wiki Sync] 文档 {document_id} 完成，'
            f'标记节点 {doc.node_id} 的 {updated} 个 Wiki 页面为过期')

    # 置文档 wiki_status=pending，原子 check-and-set 防抖派发节点构建任务
    Document.objects.filter(id=document_id).update(wiki_status='pending')
    from apps.wiki.tasks import build_node_wiki_task
    dispatched = KnowledgeNode.objects.filter(
        id=doc.node_id, is_deleted=False, wiki_pending=False
    ).update(wiki_pending=True)
    if dispatched:
        build_node_wiki_task.delay(doc.node_id)
        logger.info(f'[Wiki Sync] 触发节点 {doc.node_id} Wiki 构建任务')
