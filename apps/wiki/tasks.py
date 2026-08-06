"""LLM Wiki Celery 任务"""
from celery import shared_task
from loguru import logger


@shared_task(queue='parse')
def generate_wiki_for_node(node_id: int):
    """为节点生成 Wiki 页面

    Args:
        node_id: 知识节点 ID
    """
    from apps.llm.factory import get_llm_advanced
    from apps.wiki.generator import generate_wiki_page
    llm = get_llm_advanced()
    page = generate_wiki_page(node_id, llm)
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
    """定时任务：刷新过期的 Wiki 页面"""
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
                count += 1
            except Exception as e:
                logger.error(f'[Wiki Task] 刷新失败 page_id={page.id}: {e}')

    logger.info(f'[Wiki Task] 刷新了 {count} 个过期 Wiki 页面')
    return count
