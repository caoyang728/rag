"""LLM Wiki Celery 任务"""
from celery import shared_task
from loguru import logger


def _set_node_docs_wiki_status(node_id: int, status: str):
    """批量更新节点下已完成文档的 wiki_status（节点级构建结果回写）

    Args:
        node_id: 知识节点 ID
        status: 目标状态 done/failed/skipped
    """
    from apps.knowledge.models import Document
    Document.objects.filter(node_id=node_id, is_deleted=False, status='done').update(wiki_status=status)


@shared_task(queue='parse')
def build_node_wiki_task(node_id: int):
    """按节点防抖合并的 Wiki 构建任务：基于节点最新文档重新生成 Wiki 页面

    1. 先清除节点待处理标记（wiki_pending），任务崩溃后不残留，后续文档可重新触发
    2. 配置关闭 → 该节点待构建文档标记 skipped
    3. 无已完成文档 → 直接返回
    4. 统一标记 extracting 后调用生成器，结果回写 done/failed（失败抛出供任务看板记录）

    Args:
        node_id: 知识节点 ID
    """
    from apps.knowledge.models import Document, KnowledgeNode

    KnowledgeNode.objects.filter(id=node_id).update(wiki_pending=False)
    from apps.wiki.sync import _wiki_enabled
    if not _wiki_enabled():
        Document.objects.filter(node_id=node_id, wiki_status='pending').update(wiki_status='skipped')
        return {'ok': True, 'processed': 0, 'skipped': True}

    docs = Document.objects.filter(node_id=node_id, is_deleted=False, status='done')
    doc_ids = list(docs.values_list('id', flat=True))
    if not doc_ids:
        return {'ok': True, 'processed': 0}

    # 统一标记 extracting，避免并发任务重复处理
    Document.objects.filter(id__in=doc_ids).update(wiki_status='extracting')

    from apps.llm.factory import get_llm_advanced
    from apps.wiki.generator import generate_wiki_page
    try:
        llm = get_llm_advanced()
        page = generate_wiki_page(node_id, llm)
        _set_node_docs_wiki_status(node_id, 'done')
    except Exception as e:
        logger.exception(f'[Wiki Task] 节点 {node_id} Wiki 构建失败: {e}')
        _set_node_docs_wiki_status(node_id, 'failed')
        raise
    logger.info(f'[Wiki Task] 节点 {node_id} 生成页面: {page.title} (id={page.id})')
    return {'ok': True, 'processed': len(doc_ids)}


@shared_task(queue='parse')
def generate_wiki_for_node(node_id: int):
    """为节点生成 Wiki 页面（手动触发，结果同步回写节点文档 wiki_status）

    Args:
        node_id: 知识节点 ID
    """
    from apps.llm.factory import get_llm_advanced
    from apps.wiki.generator import generate_wiki_page
    llm = get_llm_advanced()
    try:
        page = generate_wiki_page(node_id, llm)
        _set_node_docs_wiki_status(node_id, 'done')
    except Exception as e:
        logger.exception(f'[Wiki Task] 手动生成失败 node_id={node_id}: {e}')
        _set_node_docs_wiki_status(node_id, 'failed')
        raise
    logger.info(f'[Wiki Task] 生成页面: {page.title} (id={page.id})')
    return page.id


@shared_task(queue='parse')
def generate_community_wiki_task(community_id: int, level: int = 0):
    """为社区生成 Wiki 页面

    Args:
        community_id: 社区编号
        level: 社区层级
    """
    from apps.llm.factory import get_llm_advanced
    from apps.wiki.generator import generate_community_wiki
    llm = get_llm_advanced()
    page = generate_community_wiki(community_id, level, llm)
    logger.info(f'[Wiki Task] 生成社区 Wiki: {page.title} (id={page.id})')
    return page.id


@shared_task(queue='parse')
def refresh_expired_wiki_pages():
    """定时任务：刷新过期的 Wiki 页面

    页面重建成功后将该节点下已完成文档的 wiki_status 回写为 done，
    单个页面失败仅标记该节点文档 failed，不影响其他页面刷新。
    """
    from apps.llm.factory import get_llm_advanced
    from apps.wiki.generator import generate_wiki_page
    from apps.wiki.models import WikiPage

    expired = WikiPage.objects.filter(status='expired').select_related('node')[:10]
    if not expired:
        return 0

    llm = get_llm_advanced()
    count = 0
    for page in expired:
        if page.node:
            try:
                generate_wiki_page(page.node_id, llm)
                _set_node_docs_wiki_status(page.node_id, 'done')
                count += 1
            except Exception as e:
                logger.error(f'[Wiki Task] 刷新失败 page_id={page.id}: {e}')
                _set_node_docs_wiki_status(page.node_id, 'failed')

    logger.info(f'[Wiki Task] 刷新了 {count} 个过期 Wiki 页面')
    return count
