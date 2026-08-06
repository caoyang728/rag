"""
实体关系抽取 Pipeline
- extract_entities_and_relations: 调用 LLM 从切片文本抽取实体和关系
- parse_llm_response: 解析 LLM 返回的 JSON（兼容 markdown 代码块 / 前后多余文字）
- save_extraction_result: 去重写入数据库
- batch_extract_for_document: 批量处理一个文档的所有切片
"""
import json
import re

from loguru import logger

from apps.graph.models import GraphEntity, GraphRelation
from apps.graph.prompts.extract import EXTRACT_PROMPT
from apps.llm.factory import get_llm

# 单次抽取送入 LLM 的最大文本长度，避免超长切片撑爆上下文
MAX_EXTRACT_CONTENT_LEN = 4000
# 实体描述上限（去重合并时防止无限增长）
MAX_DESCRIPTION_LEN = 2000

_VALID_TYPES = {t for t, _ in GraphEntity.TYPE_CHOICES}


def extract_entities_and_relations(content: str, llm=None) -> dict:
    """调用 LLM 从文本中抽取实体和关系。

    Args:
        content: 文档切片文本
        llm: LLM 实例，为 None 时通过 get_llm() 获取

    Returns:
        {'entities': [{'name','type','description'}], 'relations': [{'source','target','type','description'}]}
    """
    if llm is None:
        llm = get_llm()

    prompt = EXTRACT_PROMPT.format(content=content[:MAX_EXTRACT_CONTENT_LEN])
    resp = llm.chat([{'role': 'user', 'content': prompt}],
                    temperature=0.1, max_tokens=2048)
    result_text = resp.get('content', '')
    return parse_llm_response(result_text)


def parse_llm_response(response_text: str) -> dict:
    """解析 LLM 返回的文本，提取 JSON 部分。

    LLM 可能输出 ```json 代码块、纯 JSON 或在 JSON 前后附带解释文字，
    这里统一收敛为 {'entities': [...], 'relations': [...]}。

    Args:
        response_text: LLM 原始返回文本

    Returns:
        dict，entities/relations 至少为 []，绝不抛异常
    """
    if not response_text:
        return {'entities': [], 'relations': []}

    text = response_text.strip()

    # 1. 优先提取 ```json ... ``` 或 ``` ... ``` 代码块内容
    code_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if code_match:
        text = code_match.group(1).strip()

    # 2. 截取第一个 { 到最后一个 }，去掉 JSON 前后的解释文字
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end > start:
        text = text[start:end + 1]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f'[Graph Extract] LLM JSON 解析失败: {e}, raw={response_text[:200]}')
        data = {}

    if not isinstance(data, dict):
        return {'entities': [], 'relations': []}

    return {
        'entities': data.get('entities') or [],
        'relations': data.get('relations') or [],
    }


def _merge_entity(existing: GraphEntity, name: str, etype: str, desc: str, doc_id: int) -> GraphEntity:
    """合并已存在实体：拼接 description、追加 source_doc_ids。

    同一实体多次被抽取时，通过 name__iexact 命中已有记录，
    描述用 '；' 拼接（去重），来源文档 ID 追加（去重）。
    """
    if desc and desc not in existing.description:
        merged = (existing.description + '；' + desc).strip('；')
        existing.description = merged[:MAX_DESCRIPTION_LEN]
    if doc_id not in existing.source_doc_ids:
        existing.source_doc_ids.append(doc_id)
    existing.save(update_fields=['description', 'source_doc_ids'])
    return existing


def _get_or_create_entity(name: str, etype: str, desc: str, doc_id: int,
                          entity_map: dict) -> tuple:
    """按名称查找或创建实体，结果缓存到 entity_map 避免重复查库。

    Returns:
        (entity, is_new): is_new=True 表示本次新建（或原 embedding 为空需同步）
    """
    if name in entity_map:
        return entity_map[name]

    existing = GraphEntity.objects.filter(name__iexact=name).first()
    if existing:
        entity = _merge_entity(existing, name, etype, desc, doc_id)
        is_new = existing.embedding is None
    else:
        entity = GraphEntity.objects.create(
            name=name, type=etype, description=desc,
            source_doc_ids=[doc_id],
        )
        is_new = True
    entity_map[name] = entity
    return entity, is_new


def save_extraction_result(chunk, result: dict) -> tuple:
    """将抽取结果保存到数据库。

    实体去重逻辑：
    - 按 name__iexact 查找已有实体，命中则合并 description 并追加 source_doc_ids
    - 未命中则创建新实体
    关系去重逻辑：
    - (source, target, relation_type) 联合唯一，已存在则合并 source_doc_ids，不重复建边

    Args:
        chunk: DocumentChunk 实例
        result: extract_entities_and_relations 的返回值

    Returns:
        (entity_objs: list[GraphEntity], relation_objs: list[GraphRelation])
    """
    doc_id = chunk.document_id
    entity_map = {}
    entity_objs = []
    # 需要同步 embedding 的实体 ID（新建或原向量缺失），末尾统一批量同步
    new_entity_ids = []

    # ---- 1. 实体去重创建/更新 ----
    for ent in result.get('entities') or []:
        name = (ent.get('name') or '').strip()
        if not name or len(name) > 256:
            continue
        etype = ent.get('type') or 'TERM'
        if etype not in _VALID_TYPES:
            etype = 'TERM'
        desc = (ent.get('description') or '').strip()[:1000]
        entity, is_new = _get_or_create_entity(name, etype, desc, doc_id, entity_map)
        entity_objs.append(entity)
        if is_new:
            new_entity_ids.append(entity.id)

    # ---- 2. 关系创建（source/target 缺失时兜底建最小实体）----
    relation_objs = []
    for rel in result.get('relations') or []:
        src_name = (rel.get('source') or '').strip()
        tgt_name = (rel.get('target') or '').strip()
        rtype = (rel.get('type') or '').strip()
        if not src_name or not tgt_name or not rtype or len(rtype) > 64:
            continue

        # 关系端点实体缺失时，按名称查找，仍不存在则创建（type=TERM）
        src, src_new = _get_or_create_entity(src_name, 'TERM', '', doc_id, entity_map)
        tgt, tgt_new = _get_or_create_entity(tgt_name, 'TERM', '', doc_id, entity_map)
        if src_new:
            new_entity_ids.append(src.id)
        if tgt_new:
            new_entity_ids.append(tgt.id)

        relation, created = GraphRelation.objects.update_or_create(
            source_entity=src,
            target_entity=tgt,
            relation_type=rtype,
            defaults={'weight': 1.0},
        )
        # 关系描述合并到 metadata，避免重复建边
        rdesc = (rel.get('description') or '').strip()
        if rdesc:
            existing_desc = relation.metadata.get('description', '')
            if rdesc not in existing_desc:
                relation.metadata['description'] = (existing_desc + '；' + rdesc)[:1000]
        if doc_id not in relation.source_doc_ids:
            relation.source_doc_ids.append(doc_id)
        relation.save(update_fields=['metadata', 'source_doc_ids'])
        relation_objs.append(relation)

    # ---- 3. 新实体 embedding 同步 ----
    # embedding 失败不影响抽取落库（抽取已成功），记录日志即可
    if new_entity_ids:
        try:
            from apps.graph.embedding import sync_entity_embeddings
            sync_entity_embeddings(new_entity_ids)
        except Exception as e:
            logger.warning(f'[Graph Extract] 实体 embedding 同步失败 ids={new_entity_ids}: {e}')

    return entity_objs, relation_objs


def batch_extract_for_document(document_id: int):
    """批量处理一个文档的所有切片：逐个切片抽取并写入图谱。

    流程：
    1. 查询该文档所有 DocumentChunk（按 chunk_index 排序）
    2. 逐 chunk 调用 extract + save，跳过过短切片（<20 字符无抽取价值）
    3. 汇总记录抽取日志

    Args:
        document_id: 文档 ID
    """
    from apps.knowledge.models import DocumentChunk

    llm = get_llm()
    chunks = DocumentChunk.objects.filter(document_id=document_id).order_by('chunk_index')

    total_entities = 0
    total_relations = 0

    for chunk in chunks:
        if len(chunk.content.strip()) < 20:
            continue
        try:
            result = extract_entities_and_relations(chunk.content, llm)
        except Exception as e:
            logger.warning(f'[Graph Extract] chunk={chunk.id} 抽取失败: {e}')
            continue

        if result.get('entities') or result.get('relations'):
            entities, relations = save_extraction_result(chunk, result)
            total_entities += len(entities)
            total_relations += len(relations)
            logger.debug(
                f'[Graph Extract] chunk={chunk.id} entities={len(entities)} relations={len(relations)}')

    logger.info(
        f'[Graph Extract] document={document_id} chunks={len(chunks)} '
        f'entities={total_entities} relations={total_relations}')
