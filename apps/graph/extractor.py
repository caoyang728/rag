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


class _JSONTruncated(Exception):
    """JSON 文本被截断的信号（容忍解析器内部使用）"""


# 数字/布尔/null 的完整 token 匹配，用于截断时判断值 token 是否完整
_NUMBER_RE = re.compile(r'-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null')


def _repair_truncated_json(text: str) -> dict:
    """修复 LLM 输出被截断导致的 JSON 解析失败。

    典型场景：max_tokens 截断使 JSON 在末尾不完整，截断点可能落在键名、
    字符串值、冒号之后、逗号/括号等任意位置，json.loads 无法直接解析
    （报错形如 "Expecting ',' delimiter" / "Expecting value" 等）。

    这里用容忍截断的递归下降解析器做尽力恢复：
    - 未闭合字符串：按可见内容吸收（值仍可用，如 "description": "服务器CO）；
    - 键名被截断 / 冒号后无值 / 值 token 不完整：丢弃该键值对；
    - 数组元素结构不完整：丢弃该元素（其内部已完整解析的键值对不受影响）；
    - 其余截断点：提前闭合当前数组/对象，返回已解析内容。

    完全无法解析时返回 {}，不中断抽取流程。
    """
    start = text.find('{')
    if start == -1:
        return {}
    s = text[start:]
    n = len(s)
    pos = 0

    def skip_ws():
        nonlocal pos
        while pos < n and s[pos] in ' \t\n\r':
            pos += 1

    def parse_string():
        """解析字符串；文本结束仍未闭合时按可见内容吸收"""
        nonlocal pos
        assert s[pos] == '"'
        pos += 1
        out = []
        while pos < n:
            ch = s[pos]
            if ch == '\\':
                if pos + 1 < n:
                    out.append(s[pos:pos + 2])
                    pos += 2
                else:
                    pos += 1  # 结尾孤立的反斜杠，丢弃
                continue
            if ch == '"':
                pos += 1
                return ''.join(out)
            out.append(ch)
            pos += 1
        return ''.join(out)

    def parse_value():
        """解析一个值；结构不完整时抛 _JSONTruncated"""
        nonlocal pos
        skip_ws()
        if pos >= n:
            raise _JSONTruncated
        ch = s[pos]
        if ch == '{':
            return parse_object()
        if ch == '[':
            return parse_array()
        if ch == '"':
            return parse_string()
        m = _NUMBER_RE.match(s, pos)
        if m:
            tok = s[pos:m.end()]
            pos = m.end()
            if tok == 'true':
                return True
            if tok == 'false':
                return False
            if tok == 'null':
                return None
            return json.loads(tok)
        raise _JSONTruncated

    def parse_array():
        """解析数组；截断时丢弃最后一个结构不完整的元素"""
        nonlocal pos
        pos += 1  # 跳过 [
        arr = []
        while True:
            skip_ws()
            if pos >= n:
                break  # 数组被截断，丢弃最后一个元素
            if s[pos] == ']':
                pos += 1
                break
            try:
                val = parse_value()
            except _JSONTruncated:
                break  # 元素结构不完整 → 丢弃该元素
            arr.append(val)
            skip_ws()
            if pos < n and s[pos] == ',':
                pos += 1
                continue
            if pos < n and s[pos] == ']':
                pos += 1
                break
            break  # 元素后缺少 , 或 ] → 截断，丢弃最后一个元素
        return arr

    def parse_object():
        """解析对象；截断时丢弃最后一个结构不完整的键值对"""
        nonlocal pos
        pos += 1  # 跳过 {
        obj = {}
        while True:
            skip_ws()
            if pos >= n:
                break  # 对象被截断
            if s[pos] == '}':
                pos += 1
                break
            if s[pos] != '"':
                break  # 键不完整/非法 → 丢弃该键值对
            key = parse_string()
            skip_ws()
            if pos >= n or s[pos] != ':':
                break  # 键被截断或缺少冒号 → 丢弃该键值对
            pos += 1  # 跳过 :
            try:
                val = parse_value()
            except _JSONTruncated:
                break  # 值缺失/不完整 → 丢弃该键值对
            obj[key] = val
            skip_ws()
            if pos < n and s[pos] == ',':
                pos += 1
                continue
            if pos < n and s[pos] == '}':
                pos += 1
                break
            break  # 键值对后缺少 , 或 } → 截断，丢弃最后一个键值对
        return obj

    try:
        val = parse_value()
    except _JSONTruncated:
        val = {}
    return val if isinstance(val, dict) else {}


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

    # 2. 截取第一个 { 之后的 JSON 部分，去掉 JSON 前的解释文字。
    #    不按最后一个 } 截尾：字符串值里可能含 '}'（如描述含花括号），
    #    按 '}' 截尾会把字符串截断；尾部多余文字/截断统一交给修复逻辑兜底。
    start = text.find('{')
    if start == -1:
        return {'entities': [], 'relations': []}

    try:
        data = json.loads(text[start:])
    except json.JSONDecodeError as e:
        # 常规解析失败后尝试截断修复（应对 max_tokens 截断或尾部多余文字）
        data = _repair_truncated_json(text[start:])
        if not data:
            logger.warning(f'[Graph Extract] LLM JSON 解析失败: {e}, raw={response_text[:200]}')

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
