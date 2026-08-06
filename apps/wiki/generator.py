"""
LLM Wiki 生成器
- generate_wiki_page: 基于知识节点下的文档生成 Wiki 页面
- generate_community_wiki: 基于图谱社区生成 Wiki 页面
"""
from django.db.models import Q
from loguru import logger

from apps.graph.models import GraphEntity, GraphRelation
from apps.llm.embedding import get_embedding_client
from apps.wiki.models import WikiPage
from apps.wiki.prompts.generate import WIKI_PAGE_PROMPT, COMMUNITY_WIKI_PROMPT

# 生成页面时的上下文采样上限
MAX_DOC_TITLES = 20
MAX_DOC_FOR_CHUNKS = 5
MAX_CHUNKS_PER_DOC = 3
MAX_CHUNK_PREVIEW_LEN = 300


def generate_wiki_page(node_id: int, llm) -> WikiPage:
    """为知识库节点生成 Wiki 页面。

    基于节点下已完成解析的文档（标题 + 前几个切片内容）作为参考信息，
    让 LLM 生成结构化 Markdown 页面并写入向量。

    Args:
        node_id: 知识节点 ID
        llm: LLM 实例

    Returns:
        生成/更新的 WikiPage
    """
    from apps.knowledge.models import KnowledgeNode, Document

    node = KnowledgeNode.objects.get(id=node_id)
    docs = Document.objects.filter(node=node, is_deleted=False, status='done')

    doc_titles = [d.title for d in docs[:MAX_DOC_TITLES]]
    chunk_contents = []
    for doc in docs[:MAX_DOC_FOR_CHUNKS]:
        chunks = doc.chunks.all()[:MAX_CHUNKS_PER_DOC]
        for c in chunks:
            chunk_contents.append(c.content[:MAX_CHUNK_PREVIEW_LEN])

    source_info = f"节点名称：{node.name}\n节点路径：{node.path}\n"
    if doc_titles:
        source_info += "\n关联文档：\n" + '\n'.join([f"- {t}" for t in doc_titles])
    if chunk_contents:
        source_info += "\n\n文档内容摘要：\n" + '\n\n'.join(chunk_contents[:5])

    prompt = WIKI_PAGE_PROMPT.format(title=node.name, source_info=source_info)
    resp = llm.chat([{'role': 'user', 'content': prompt}],
                    temperature=0.3, max_tokens=4096)
    content = resp.get('content', '')

    page, _ = WikiPage.objects.update_or_create(
        node=node,
        defaults={
            'title': node.name,
            'content': content,
            'summary': content[:300] if len(content) > 300 else content,
            'status': 'published',
            'tags': [node.root_type],
        }
    )

    # 生成 embedding
    _sync_wiki_embedding(page)

    logger.info(f'[Wiki] 生成页面: {page.title} (id={page.id})')
    return page


def generate_community_wiki(community_id: int, level: int, llm) -> WikiPage:
    """为社区生成 Wiki 页面。

    基于社区的实体、关系信息生成 Wiki 页面，页面对应 GraphCommunity 记录。

    Args:
        community_id: 社区编号（GraphCommunity.community_id）
        level: 社区层级
        llm: LLM 实例

    Returns:
        生成/更新的 WikiPage
    """
    from apps.graph.models import GraphCommunity

    community = GraphCommunity.objects.get(community_id=community_id, level=level)
    entities = GraphEntity.objects.filter(id__in=community.entity_ids)
    relations = GraphRelation.objects.filter(
        Q(source_entity_id__in=community.entity_ids) & Q(target_entity_id__in=community.entity_ids)
    ).select_related('source_entity', 'target_entity')

    entities_text = '\n'.join([
        f"- {e.name}（{e.get_type_display()}）：{e.description[:100]}" for e in entities
    ]) or "无"

    relations_text = '\n'.join([
        f"- {r.source_entity.name} --[{r.relation_type}]--> {r.target_entity.name}"
        for r in relations[:20]
    ]) or "无"

    prompt = COMMUNITY_WIKI_PROMPT.format(
        topic=community.metadata.get('topic', '未命名'),
        summary=community.summary,
        keywords=', '.join(community.keywords[:10]),
        entities_text=entities_text,
        relations_text=relations_text,
    )

    resp = llm.chat([{'role': 'user', 'content': prompt}],
                    temperature=0.3, max_tokens=4096)
    content = resp.get('content', '')

    page, _ = WikiPage.objects.update_or_create(
        community=community,
        defaults={
            'title': community.metadata.get('topic', f'知识领域-{community.community_id}'),
            'content': content,
            'summary': content[:300] if len(content) > 300 else content,
            'status': 'published',
            'tags': community.keywords[:5],
        }
    )

    _sync_wiki_embedding(page)

    logger.info(f'[Wiki] 生成社区 Wiki: {page.title} (id={page.id})')
    return page


def _sync_wiki_embedding(page: WikiPage):
    """同步 Wiki 页面的 embedding。

    用标题 + 摘要生成向量（正文过长且含 Markdown 标记，标题+摘要已能表达主题）。
    """
    embed_client = get_embedding_client()
    vec = embed_client.embed_one(f"{page.title}\n{page.summary}")
    if vec and not all(v == 0.0 for v in vec):
        page.embedding = vec
        page.save(update_fields=['embedding'])
