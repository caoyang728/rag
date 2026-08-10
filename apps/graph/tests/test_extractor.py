"""
apps.graph.extractor 单元/集成测试 —— 实体关系抽取 Pipeline

覆盖范围：
- parse_llm_response：纯 JSON / 代码块 / 前后附带文字 / 畸形输入 / 非 dict 类型
- extract_entities_and_relations：LLM 调用 + 内容截断 + 自定义 LLM 透传
- save_extraction_result：新建实体 / 合并去重 / 跳过非法数据 / 关系创建与合并
- batch_extract_for_document：切片迭代 / 短切片跳过 / 抽取异常隔离

测试分层：
- parse_llm_response / extract_entities_and_relations / batch_extract_for_document：
  纯 mock，隔离 LLM 与 DB
- save_extraction_result：DB 集成测试，验证实体/关系去重落库逻辑
"""
import json
import pytest
from unittest.mock import patch, MagicMock


from apps.graph.models import GraphEntity, GraphRelation


# ============================================================================
# parse_llm_response：纯函数，各种输入格式
# ============================================================================
@pytest.mark.unit
def test_parse_empty():
    """空字符串应返回空结构"""
    from apps.graph.extractor import parse_llm_response
    assert parse_llm_response('') == {'entities': [], 'relations': []}


@pytest.mark.unit
def test_parse_none():
    """None 输入应返回空结构（not response_text 短路）"""
    from apps.graph.extractor import parse_llm_response
    assert parse_llm_response(None) == {'entities': [], 'relations': []}


@pytest.mark.unit
def test_parse_plain_json():
    """纯 JSON 应正确解析"""
    from apps.graph.extractor import parse_llm_response
    text = json.dumps({
        'entities': [{'name': '张三', 'type': 'PERSON', 'description': '员工'}],
        'relations': [{'source': '张三', 'target': 'HR', 'type': '隶属于'}],
    })
    result = parse_llm_response(text)
    assert len(result['entities']) == 1
    assert result['entities'][0]['name'] == '张三'
    assert len(result['relations']) == 1
    assert result['relations'][0]['type'] == '隶属于'


@pytest.mark.unit
def test_parse_json_code_block():
    """```json 代码块应正确提取 JSON"""
    from apps.graph.extractor import parse_llm_response
    inner = json.dumps({
        'entities': [{'name': '李四', 'type': 'ORG'}],
        'relations': [],
    })
    text = f'```json\n{inner}\n```'
    result = parse_llm_response(text)
    assert len(result['entities']) == 1
    assert result['entities'][0]['name'] == '李四'


@pytest.mark.unit
def test_parse_plain_code_block():
    """``` 代码块（无 json 标记）也应正确提取"""
    from apps.graph.extractor import parse_llm_response
    inner = json.dumps({'entities': [{'name': '王五'}], 'relations': []})
    text = f'```\n{inner}\n```'
    result = parse_llm_response(text)
    assert len(result['entities']) == 1
    assert result['entities'][0]['name'] == '王五'


@pytest.mark.unit
def test_parse_with_surrounding_text():
    """JSON 前后有解释文字时应截取 { 到 } 的部分"""
    from apps.graph.extractor import parse_llm_response
    text = '以下是抽取结果：\n{"entities": [{"name": "赵六"}], "relations": []}\n以上是结果。'
    result = parse_llm_response(text)
    assert len(result['entities']) == 1
    assert result['entities'][0]['name'] == '赵六'


@pytest.mark.unit
def test_parse_malformed_json():
    """非 JSON 文本应返回空结构（不抛异常）"""
    from apps.graph.extractor import parse_llm_response
    result = parse_llm_response('这不是JSON')
    assert result == {'entities': [], 'relations': []}


@pytest.mark.unit
def test_parse_non_dict_json():
    """JSON 为数组（非 dict）时应返回空结构"""
    from apps.graph.extractor import parse_llm_response
    result = parse_llm_response('[1, 2, 3]')
    assert result == {'entities': [], 'relations': []}


@pytest.mark.unit
def test_parse_missing_keys():
    """JSON 缺少 entities/relations 键时应补全为空列表"""
    from apps.graph.extractor import parse_llm_response
    result = parse_llm_response('{"other": "value"}')
    assert result == {'entities': [], 'relations': []}


@pytest.mark.unit
def test_parse_null_values():
    """entities/relations 为 null 时应补全为空列表"""
    from apps.graph.extractor import parse_llm_response
    result = parse_llm_response('{"entities": null, "relations": null}')
    assert result == {'entities': [], 'relations': []}


# ============================================================================
# parse_llm_response：max_tokens 截断场景（容忍解析器修复）
# ============================================================================
@pytest.mark.unit
def test_parse_truncated_mid_string_value():
    """字符串值内部被截断（用户场景："description": "服务器CO）应吸收可见内容并保留实体"""
    from apps.graph.extractor import parse_llm_response
    text = ('{ \n'
            '   "entities": [ \n'
            '     {"name": "server.cors.allowed_methods", "type": "TERM", '
            '"description": "服务器CORS配置中允许的HTTP方法列表"}, \n'
            '     {"name": "server.cors.allowed_headers", "type": "TERM", '
            '"description": "服务器CO')
    result = parse_llm_response(text)
    assert len(result['entities']) == 2
    assert result['entities'][1]['name'] == 'server.cors.allowed_headers'
    # 截断的字符串值按可见内容吸收
    assert result['entities'][1]['description'] == '服务器CO'


@pytest.mark.unit
def test_parse_truncated_mid_key():
    """键名内部被截断（如 "desc）应丢弃该键值对，保留已完整解析的成员"""
    from apps.graph.extractor import parse_llm_response
    text = '{"entities": [{"name": "a", "type": "TERM", "desc'
    result = parse_llm_response(text)
    assert len(result['entities']) == 1
    assert result['entities'][0]['name'] == 'a'
    assert result['entities'][0]['type'] == 'TERM'
    assert 'description' not in result['entities'][0]


@pytest.mark.unit
def test_parse_truncated_after_colon():
    """冒号后值未开始（"description": 后截断）应丢弃该键值对，保留其余成员"""
    from apps.graph.extractor import parse_llm_response
    text = '{"entities": [{"name": "a", "type": "TERM", "description": '
    result = parse_llm_response(text)
    assert len(result['entities']) == 1
    assert result['entities'][0]['name'] == 'a'
    assert 'description' not in result['entities'][0]


@pytest.mark.unit
def test_parse_truncated_mid_array_element():
    """数组末尾元素结构不完整时，已完整解析的前置元素应全部保留"""
    from apps.graph.extractor import parse_llm_response
    text = ('{"entities": ['
            '{"name": "a", "type": "TERM"}, '
            '{"name": "b", "type": "TERM"}, '
            '{"name": "c", "type": "TER')
    result = parse_llm_response(text)
    names = [e.get('name') for e in result['entities']]
    assert names == ['a', 'b', 'c']


@pytest.mark.unit
def test_parse_truncated_in_relations():
    """relations 数组被截断时，已解析的 entities 应完整保留"""
    from apps.graph.extractor import parse_llm_response
    text = ('{"entities": [{"name": "a", "type": "TERM"}], '
            '"relations": [{"source": "a", "target": "b", "type": "依赖", '
            '"description": "a依赖')
    result = parse_llm_response(text)
    assert len(result['entities']) == 1
    assert len(result['relations']) == 1
    assert result['relations'][0]['description'] == 'a依赖'


@pytest.mark.unit
def test_parse_json_with_brace_in_string():
    """描述字符串内含 } 不应导致 JSON 被按最后一个 } 错误截尾"""
    from apps.graph.extractor import parse_llm_response
    text = ('{"entities": [{"name": "http_method", "type": "TERM", '
            '"description": "支持{GET, POST}等方法"}], "relations": []}')
    result = parse_llm_response(text)
    assert len(result['entities']) == 1
    assert result['entities'][0]['description'] == '支持{GET, POST}等方法'


@pytest.mark.unit
def test_parse_truncated_escaped_quote_in_string():
    """字符串含转义引号 \\" 时截断，应正确吸收且不破坏结构"""
    from apps.graph.extractor import parse_llm_response
    text = ('{"entities": [{"name": "a", "type": "TERM", '
            '"description": "他说:\\"你好\\"，然后')
    result = parse_llm_response(text)
    assert len(result['entities']) == 1
    assert result['entities'][0]['name'] == 'a'
    assert '他说' in result['entities'][0]['description']


# ============================================================================
# extract_entities_and_relations：mock LLM
# ============================================================================
@pytest.mark.unit
@patch('apps.graph.extractor.get_llm')
def test_extract_calls_llm(mock_get_llm):
    """未传 llm 时应通过 get_llm() 获取默认 LLM 实例"""
    from apps.graph.extractor import extract_entities_and_relations

    mock_llm = MagicMock()
    mock_llm.chat.return_value = {'content': '{"entities": [], "relations": []}'}
    mock_get_llm.return_value = mock_llm

    result = extract_entities_and_relations('测试内容')

    mock_get_llm.assert_called_once()
    mock_llm.chat.assert_called_once()
    assert result == {'entities': [], 'relations': []}


@pytest.mark.unit
def test_extract_with_provided_llm():
    """传入 llm 时不应调用 get_llm()"""
    from apps.graph.extractor import extract_entities_and_relations

    mock_llm = MagicMock()
    mock_llm.chat.return_value = {
        'content': '{"entities": [{"name": "张三", "type": "PERSON"}], "relations": []}'
    }

    result = extract_entities_and_relations('测试', llm=mock_llm)

    assert len(result['entities']) == 1
    assert result['entities'][0]['name'] == '张三'


@pytest.mark.unit
def test_extract_truncates_long_content():
    """超长内容应被截断到 MAX_EXTRACT_CONTENT_LEN，避免 prompt 超长"""
    from apps.graph.extractor import MAX_EXTRACT_CONTENT_LEN, extract_entities_and_relations

    mock_llm = MagicMock()
    mock_llm.chat.return_value = {'content': '{}'}

    long_content = 'A' * (MAX_EXTRACT_CONTENT_LEN + 200)
    extract_entities_and_relations(long_content, llm=mock_llm)

    # 验证传给 LLM 的 prompt 中内容被截断
    call_args = mock_llm.chat.call_args
    messages = call_args[0][0]
    prompt = messages[0]['content']
    # 截断后的内容长度不应超过原始内容
    assert 'A' * MAX_EXTRACT_CONTENT_LEN in prompt
    assert 'A' * (MAX_EXTRACT_CONTENT_LEN + 200) not in prompt


@pytest.mark.unit
def test_extract_llm_call_params():
    """LLM 调用应使用 temperature=0.1, max_tokens=2048"""
    from apps.graph.extractor import extract_entities_and_relations

    mock_llm = MagicMock()
    mock_llm.chat.return_value = {'content': '{}'}

    extract_entities_and_relations('测试', llm=mock_llm)

    call_kwargs = mock_llm.chat.call_args[1]
    assert call_kwargs['temperature'] == 0.1
    assert call_kwargs['max_tokens'] == 2048


# ============================================================================
# save_extraction_result：DB 集成测试
# ============================================================================
@pytest.mark.django_db
class TestSaveExtractionResult:
    """save_extraction_result DB 集成测试

    验证实体/关系去重落库：
    - 新建实体（name__iexact 未命中）
    - 合并已存在实体（description 拼接 + source_doc_ids 追加）
    - 跳过非法实体（空名/超长名/非法类型）
    - 关系创建（端点实体缺失时自动以 TERM 类型创建）
    - 跳过非法关系（缺 source/target/type）
    - 关系描述合并到 metadata
    """

    @pytest.fixture(autouse=True)
    def _chunk(self):
        """pytest fixture：mock chunk（仅需 document_id 字段）"""
        self.chunk = MagicMock()
        self.chunk.document_id = 100

    @patch('apps.graph.embedding.sync_entity_embeddings')
    def test_creates_new_entities(self, mock_sync):
        """新实体应被创建到 GraphEntity 表"""
        from apps.graph.extractor import save_extraction_result

        result = {
            'entities': [
                {'name': '张三', 'type': 'PERSON', 'description': '研发员工'},
                {'name': 'HR部门', 'type': 'ORG', 'description': '人事部门'},
            ],
            'relations': [],
        }
        entities, relations = save_extraction_result(self.chunk, result)

        assert len(entities) == 2
        assert GraphEntity.objects.count() == 2
        # 验证 source_doc_ids 包含文档 ID
        for ent in entities:
            assert 100 in ent.source_doc_ids

    @patch('apps.graph.embedding.sync_entity_embeddings')
    def test_merges_existing_entity_by_iexact(self, mock_sync):
        """同名实体（name__iexact 不区分大小写）应合并 description 与 source_doc_ids"""
        from apps.graph.extractor import save_extraction_result

        # 第一次抽取：文档 100
        result1 = {
            'entities': [{'name': '张三', 'type': 'PERSON', 'description': '员工'}],
            'relations': [],
        }
        save_extraction_result(self.chunk, result1)

        # 第二次抽取：文档 200，同名实体（不同大小写）
        chunk2 = MagicMock()
        chunk2.document_id = 200
        result2 = {
            'entities': [{'name': '张三', 'type': 'PERSON', 'description': '经理'}],
            'relations': [],
        }
        entities, _ = save_extraction_result(chunk2, result2)

        # 不应创建新实体
        assert GraphEntity.objects.count() == 1
        entity = GraphEntity.objects.first()
        # description 应被合并（'；' 分隔）
        assert '员工' in entity.description
        assert '经理' in entity.description
        # source_doc_ids 应包含两个文档 ID
        assert 100 in entity.source_doc_ids
        assert 200 in entity.source_doc_ids

    @patch('apps.graph.embedding.sync_entity_embeddings')
    def test_skips_invalid_entities(self, mock_sync):
        """非法实体（空名/超长名/非法类型）应被跳过"""
        from apps.graph.extractor import save_extraction_result

        result = {
            'entities': [
                {'name': '', 'type': 'PERSON', 'description': '空名'},  # 空名 -> 跳过
                {'name': 'A' * 300, 'type': 'PERSON', 'description': '超长名'},  # >256 -> 跳过
                {'name': '有效实体', 'type': 'INVALID_TYPE', 'description': '非法类型降级为TERM'},
            ],
            'relations': [],
        }
        entities, _ = save_extraction_result(self.chunk, result)

        # 只有第三个实体被创建（前两个跳过）
        assert len(entities) == 1
        assert entities[0].name == '有效实体'
        # 非法类型降级为 TERM
        assert entities[0].type == 'TERM'

    @patch('apps.graph.embedding.sync_entity_embeddings')
    def test_creates_relations_with_auto_entities(self, mock_sync):
        """关系端点实体缺失时应自动以 TERM 类型创建"""
        from apps.graph.extractor import save_extraction_result

        result = {
            'entities': [],
            'relations': [
                {'source': '张三', 'target': '李四', 'type': '认识', 'description': '同事关系'},
            ],
        }
        entities, relations = save_extraction_result(self.chunk, result)

        # 1 条关系
        assert len(relations) == 1
        assert GraphRelation.objects.count() == 1
        # 端点实体自动创建（TERM 类型）
        assert GraphEntity.objects.count() == 2
        rel = relations[0]
        assert rel.relation_type == '认识'
        assert rel.source_entity.name == '张三'
        assert rel.target_entity.name == '李四'
        # 关系描述存入 metadata
        assert 'description' in rel.metadata
        assert '同事关系' in rel.metadata['description']

    @patch('apps.graph.embedding.sync_entity_embeddings')
    def test_skips_invalid_relations(self, mock_sync):
        """非法关系（缺 source/target/type 或 type 超长）应被跳过"""
        from apps.graph.extractor import save_extraction_result

        result = {
            'entities': [],
            'relations': [
                {'source': '', 'target': '李四', 'type': '认识'},  # 缺 source -> 跳过
                {'source': '张三', 'target': '', 'type': '认识'},  # 缺 target -> 跳过
                {'source': '张三', 'target': '李四', 'type': ''},  # 缺 type -> 跳过
                {'source': '张三', 'target': '李四', 'type': 'A' * 100},  # type >64 -> 跳过
            ],
        }
        entities, relations = save_extraction_result(self.chunk, result)

        # 全部关系被跳过
        assert len(relations) == 0
        assert GraphRelation.objects.count() == 0
        # 不会因为关系端点创建任何实体
        assert GraphEntity.objects.count() == 0

    @patch('apps.graph.embedding.sync_entity_embeddings')
    def test_relation_dedup_by_unique_together(self, mock_sync):
        """相同 (source, target, type) 的关系应去重（update_or_create）"""
        from apps.graph.extractor import save_extraction_result

        result = {
            'entities': [],
            'relations': [
                {'source': '张三', 'target': '李四', 'type': '认识', 'description': '第一次描述'},
            ],
        }
        save_extraction_result(self.chunk, result)

        # 第二次抽取相同关系
        chunk2 = MagicMock()
        chunk2.document_id = 200
        save_extraction_result(chunk2, result)

        # 只应有 1 条关系（unique_together 去重）
        assert GraphRelation.objects.count() == 1
        rel = GraphRelation.objects.first()
        # source_doc_ids 应包含两个文档
        assert 100 in rel.source_doc_ids
        assert 200 in rel.source_doc_ids
        # 描述应被合并
        assert '第一次描述' in rel.metadata.get('description', '')

    @patch('apps.graph.embedding.sync_entity_embeddings')
    def test_entity_description_truncated(self, mock_sync):
        """实体描述应被截断到 1000 字符（[:1000]）"""
        from apps.graph.extractor import save_extraction_result

        long_desc = 'A' * 1500
        result = {
            'entities': [{'name': '测试', 'type': 'TERM', 'description': long_desc}],
            'relations': [],
        }
        save_extraction_result(self.chunk, result)

        entity = GraphEntity.objects.first()
        assert len(entity.description) == 1000

    @patch('apps.graph.embedding.sync_entity_embeddings')
    def test_relation_description_merging(self, mock_sync):
        """同一关系多次抽取不同描述时，应合并到 metadata（'；' 分隔，去重）"""
        from apps.graph.extractor import save_extraction_result

        # 第一次：描述 A
        result1 = {
            'entities': [],
            'relations': [
                {'source': 'A', 'target': 'B', 'type': '认识', 'description': '描述A'},
            ],
        }
        save_extraction_result(self.chunk, result1)

        # 第二次：描述 B（不同描述，应合并）
        chunk2 = MagicMock()
        chunk2.document_id = 200
        result2 = {
            'entities': [],
            'relations': [
                {'source': 'A', 'target': 'B', 'type': '认识', 'description': '描述B'},
            ],
        }
        save_extraction_result(chunk2, result2)

        rel = GraphRelation.objects.first()
        desc = rel.metadata.get('description', '')
        assert '描述A' in desc
        assert '描述B' in desc

    @patch('apps.graph.embedding.sync_entity_embeddings')
    def test_embedding_sync_called_for_new_entities(self, mock_sync):
        """新建实体应触发 embedding 同步"""
        from apps.graph.extractor import save_extraction_result

        result = {
            'entities': [{'name': '新实体', 'type': 'PERSON', 'description': '描述'}],
            'relations': [],
        }
        save_extraction_result(self.chunk, result)

        mock_sync.assert_called_once()

    @patch('apps.graph.embedding.sync_entity_embeddings')
    def test_embedding_sync_failure_non_blocking(self, mock_sync):
        """embedding 同步失败不应阻断抽取落库"""
        from apps.graph.extractor import save_extraction_result

        mock_sync.side_effect = Exception('Embedding service unavailable')

        result = {
            'entities': [{'name': '测试', 'type': 'PERSON', 'description': '描述'}],
            'relations': [],
        }
        # 不应抛异常
        entities, _ = save_extraction_result(self.chunk, result)

        # 实体仍应被创建
        assert len(entities) == 1
        assert GraphEntity.objects.count() == 1


# ============================================================================
# batch_extract_for_document：mock DocumentChunk + extract + save
# ============================================================================
@pytest.mark.unit
@patch('apps.graph.extractor.save_extraction_result')
@patch('apps.graph.extractor.extract_entities_and_relations')
@patch('apps.graph.extractor.get_llm')
@patch('apps.knowledge.models.DocumentChunk')
def test_batch_extract_iterates_chunks(mock_chunk_cls, mock_get_llm, mock_extract, mock_save):
    """应遍历文档所有切片并调用抽取"""
    from apps.graph.extractor import batch_extract_for_document

    # 内容长度需 >= 20 字符（否则被短切片过滤跳过）
    mock_chunks = [
        MagicMock(id=1, content='A' * 25, chunk_index=0),
        MagicMock(id=2, content='B' * 25, chunk_index=1),
    ]
    mock_chunk_cls.objects.filter.return_value.order_by.return_value = mock_chunks

    mock_extract.return_value = {'entities': [{'name': 'x'}], 'relations': []}
    mock_save.return_value = ([MagicMock()], [])

    batch_extract_for_document(1)

    assert mock_extract.call_count == 2
    assert mock_save.call_count == 2


@pytest.mark.unit
@patch('apps.graph.extractor.save_extraction_result')
@patch('apps.graph.extractor.extract_entities_and_relations')
@patch('apps.graph.extractor.get_llm')
@patch('apps.knowledge.models.DocumentChunk')
def test_batch_extract_skips_short_chunks_explicit(mock_chunk_cls, mock_get_llm, mock_extract, mock_save):
    """明确验证：<20 字符的切片被跳过，>=20 字符的切片被处理"""
    from apps.graph.extractor import batch_extract_for_document

    long_content = 'A' * 25  # 25 chars, >= 20
    short_content = 'B' * 15  # 15 chars, < 20
    mock_chunks = [
        MagicMock(id=1, content=long_content, chunk_index=0),
        MagicMock(id=2, content=short_content, chunk_index=1),
        MagicMock(id=3, content=long_content, chunk_index=2),
    ]
    mock_chunk_cls.objects.filter.return_value.order_by.return_value = mock_chunks

    mock_extract.return_value = {'entities': [{'name': 'x'}], 'relations': []}
    mock_save.return_value = ([MagicMock()], [])

    batch_extract_for_document(1)

    # 只有切片 1 和 3 被处理（切片 2 太短跳过）
    assert mock_extract.call_count == 2


@pytest.mark.unit
@patch('apps.graph.extractor.save_extraction_result')
@patch('apps.graph.extractor.extract_entities_and_relations')
@patch('apps.graph.extractor.get_llm')
@patch('apps.knowledge.models.DocumentChunk')
def test_batch_extract_handles_extraction_error(mock_chunk_cls, mock_get_llm, mock_extract, mock_save):
    """单个切片抽取异常不应中断整个批次"""
    from apps.graph.extractor import batch_extract_for_document

    mock_chunks = [
        MagicMock(id=1, content='A' * 25, chunk_index=0),
        MagicMock(id=2, content='B' * 25, chunk_index=1),
    ]
    mock_chunk_cls.objects.filter.return_value.order_by.return_value = mock_chunks

    # 第一个切片抽取抛异常，第二个正常
    mock_extract.side_effect = [Exception('LLM error'),
                                {'entities': [{'name': 'x'}], 'relations': []}]
    mock_save.return_value = ([MagicMock()], [])

    # 不应抛异常
    batch_extract_for_document(1)

    # 第二个切片仍被处理
    assert mock_save.call_count == 1


@pytest.mark.unit
@patch('apps.graph.extractor.save_extraction_result')
@patch('apps.graph.extractor.extract_entities_and_relations')
@patch('apps.graph.extractor.get_llm')
@patch('apps.knowledge.models.DocumentChunk')
def test_batch_extract_skips_empty_results(mock_chunk_cls, mock_get_llm, mock_extract, mock_save):
    """抽取结果为空（无实体无关系）时不应调用 save_extraction_result"""
    from apps.graph.extractor import batch_extract_for_document

    mock_chunks = [
        MagicMock(id=1, content='A' * 25, chunk_index=0),
    ]
    mock_chunk_cls.objects.filter.return_value.order_by.return_value = mock_chunks

    mock_extract.return_value = {'entities': [], 'relations': []}

    batch_extract_for_document(1)

    mock_save.assert_not_called()


@pytest.mark.unit
@patch('apps.graph.extractor.save_extraction_result')
@patch('apps.graph.extractor.extract_entities_and_relations')
@patch('apps.graph.extractor.get_llm')
@patch('apps.knowledge.models.DocumentChunk')
def test_batch_extract_no_chunks(mock_chunk_cls, mock_get_llm, mock_extract, mock_save):
    """文档无切片时不应调用抽取或保存"""
    from apps.graph.extractor import batch_extract_for_document

    mock_chunk_cls.objects.filter.return_value.order_by.return_value = []

    batch_extract_for_document(999)

    mock_extract.assert_not_called()
    mock_save.assert_not_called()


@pytest.mark.unit
@patch('apps.graph.extractor.save_extraction_result')
@patch('apps.graph.extractor.extract_entities_and_relations')
@patch('apps.graph.extractor.get_llm')
@patch('apps.knowledge.models.DocumentChunk')
def test_batch_extract_chunks_ordered(mock_chunk_cls, mock_get_llm, mock_extract, mock_save):
    """切片应按 chunk_index 排序处理"""
    from apps.graph.extractor import batch_extract_for_document

    mock_chunk_cls.objects.filter.return_value.order_by.return_value = []
    batch_extract_for_document(1)

    # 验证 order_by('chunk_index') 被调用
    mock_chunk_cls.objects.filter.return_value.order_by.assert_called_once_with('chunk_index')
