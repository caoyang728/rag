"""
apps.retrieval.rerank 单元测试 —— Rerank 精排

覆盖范围：
- rerank_docs：空 docs / 无 hits 降级 / 正常映射 / top_k 截断 / 越界 index 跳过 / 字段保留

用纯 mock（不依赖 DB）：
rerank_docs 仅依赖 get_embedding_client 返回的 client.rerank()，不涉及 ORM。
mock get_embedding_client 后可隔离 SiliconFlow API 调用，专注验证映射与降级逻辑。
"""
import pytest
from unittest.mock import patch, MagicMock


@pytest.mark.unit
def test_rerank_empty_docs():
    """空 docs 列表应直接返回空列表，不调用 embedding client"""
    from apps.retrieval.rerank import rerank_docs
    with patch('apps.retrieval.rerank.get_embedding_client') as mock_get:
        result = rerank_docs('query', [])
        assert result == []
        mock_get.assert_not_called()


@pytest.mark.unit
@patch('apps.retrieval.rerank.get_embedding_client')
def test_rerank_no_hits(mock_get_client):
    """client.rerank 返回空列表时，降级返回 docs[:top_k]"""
    from apps.retrieval.rerank import rerank_docs
    mock_client = MagicMock()
    mock_client.rerank.return_value = []
    mock_get_client.return_value = mock_client

    docs = [
        {'content': '文档1', 'chunk_id': 1},
        {'content': '文档2', 'chunk_id': 2},
        {'content': '文档3', 'chunk_id': 3},
    ]
    result = rerank_docs('query', docs, top_k=2)

    # 降级：返回 docs 前 top_k 个
    assert len(result) == 2
    assert result[0]['chunk_id'] == 1
    assert result[1]['chunk_id'] == 2
    # 降级路径不添加 rerank_score
    assert 'rerank_score' not in result[0]


@pytest.mark.unit
@patch('apps.retrieval.rerank.get_embedding_client')
def test_rerank_success(mock_get_client):
    """client.rerank 返回 hits 时，应映射回原 docs 并附加 rerank_score"""
    from apps.retrieval.rerank import rerank_docs
    mock_client = MagicMock()
    # hit 0 -> doc[0], hit 2 -> doc[2]（跳过 doc[1]）
    mock_client.rerank.return_value = [
        {'index': 0, 'score': 0.95},
        {'index': 2, 'score': 0.80},
    ]
    mock_get_client.return_value = mock_client

    docs = [
        {'content': '文档A', 'chunk_id': 10},
        {'content': '文档B', 'chunk_id': 20},
        {'content': '文档C', 'chunk_id': 30},
    ]
    result = rerank_docs('query', docs, top_k=5)

    assert len(result) == 2
    # 第一个 hit 映射到 doc[0]
    assert result[0]['chunk_id'] == 10
    assert result[0]['rerank_score'] == pytest.approx(0.95, rel=1e-6)
    # 第二个 hit 映射到 doc[2]
    assert result[1]['chunk_id'] == 30
    assert result[1]['rerank_score'] == pytest.approx(0.80, rel=1e-6)


@pytest.mark.unit
@patch('apps.retrieval.rerank.get_embedding_client')
def test_rerank_top_k(mock_get_client):
    """top_k 应透传给 client.rerank，由 API 端限制返回数量"""
    from apps.retrieval.rerank import rerank_docs
    mock_client = MagicMock()
    # client.rerank 按 top_k 返回对应数量的 hits（API 端保证）
    mock_client.rerank.return_value = [
        {'index': i, 'score': 0.9 - i * 0.1} for i in range(3)
    ]
    mock_get_client.return_value = mock_client

    docs = [{'content': f'文档{i}', 'chunk_id': i} for i in range(5)]
    result = rerank_docs('query', docs, top_k=3)

    assert len(result) == 3
    # 验证 top_k 被传递给 client.rerank
    call_kwargs = mock_client.rerank.call_args[1]
    assert call_kwargs['top_k'] == 3


@pytest.mark.unit
@patch('apps.retrieval.rerank.get_embedding_client')
def test_rerank_index_out_of_range(mock_get_client):
    """hit 的 index 超出 docs 范围时应被跳过，不引发异常"""
    from apps.retrieval.rerank import rerank_docs
    mock_client = MagicMock()
    mock_client.rerank.return_value = [
        {'index': 0, 'score': 0.9},   # 有效
        {'index': 5, 'score': 0.8},   # 越界（docs 只有 2 个）
        {'index': -1, 'score': 0.7},  # 负数，同样越界
    ]
    mock_get_client.return_value = mock_client

    docs = [
        {'content': '文档1', 'chunk_id': 1},
        {'content': '文档2', 'chunk_id': 2},
    ]
    result = rerank_docs('query', docs, top_k=5)

    # 只有 index=0 有效，其余两个越界被跳过
    assert len(result) == 1
    assert result[0]['chunk_id'] == 1


@pytest.mark.unit
@patch('apps.retrieval.rerank.get_embedding_client')
def test_rerank_preserves_doc_fields(mock_get_client):
    """rerank 结果应保留原 doc 的所有字段，并新增 rerank_score"""
    from apps.retrieval.rerank import rerank_docs
    mock_client = MagicMock()
    mock_client.rerank.return_value = [
        {'index': 0, 'score': 0.92},
    ]
    mock_get_client.return_value = mock_client

    docs = [
        {
            'content': '完整文档',
            'chunk_id': 42,
            'document_id': 100,
            'node_path': '/1/2/',
            'visibility_level': 'TEAM_ONLY',
            'score': 0.5,  # 原 RRF 分数
        },
    ]
    result = rerank_docs('query', docs, top_k=5)

    assert len(result) == 1
    doc = result[0]
    # 原字段全部保留
    assert doc['content'] == '完整文档'
    assert doc['chunk_id'] == 42
    assert doc['document_id'] == 100
    assert doc['node_path'] == '/1/2/'
    assert doc['visibility_level'] == 'TEAM_ONLY'
    assert doc['score'] == 0.5
    # 新增 rerank_score
    assert doc['rerank_score'] == pytest.approx(0.92, rel=1e-6)


@pytest.mark.unit
@patch('apps.retrieval.rerank.get_embedding_client')
def test_rerank_score_float_conversion(mock_get_client):
    """rerank_score 应被转换为 float 类型（兼容 int/str 输入）"""
    from apps.retrieval.rerank import rerank_docs
    mock_client = MagicMock()
    mock_client.rerank.return_value = [
        {'index': 0, 'score': '0.85'},  # 字符串分数
    ]
    mock_get_client.return_value = mock_client

    docs = [{'content': 'doc', 'chunk_id': 1}]
    result = rerank_docs('query', docs, top_k=5)

    assert isinstance(result[0]['rerank_score'], float)
    assert result[0]['rerank_score'] == pytest.approx(0.85, rel=1e-6)


@pytest.mark.unit
@patch('apps.retrieval.rerank.get_embedding_client')
def test_rerank_content_extraction(mock_get_client):
    """应从 docs 中提取 content 字段作为 texts 传给 client.rerank"""
    from apps.retrieval.rerank import rerank_docs
    mock_client = MagicMock()
    mock_client.rerank.return_value = []
    mock_get_client.return_value = mock_client

    docs = [
        {'content': '内容A', 'chunk_id': 1},
        {'chunk_id': 2},  # 无 content 字段
        {'content': '', 'chunk_id': 3},  # 空 content
    ]
    rerank_docs('query', docs, top_k=5)

    # 验证传给 client.rerank 的 texts 参数
    call_args = mock_client.rerank.call_args
    texts = call_args[0][1]  # 第二个位置参数
    assert texts == ['内容A', '', '']


@pytest.mark.unit
@patch('apps.retrieval.rerank.get_embedding_client')
def test_rerank_does_not_mutate_original_docs(mock_get_client):
    """rerank 不应修改原始 docs 列表中的 dict（dict(docs[idx]) 浅拷贝）"""
    from apps.retrieval.rerank import rerank_docs
    mock_client = MagicMock()
    mock_client.rerank.return_value = [
        {'index': 0, 'score': 0.9},
    ]
    mock_get_client.return_value = mock_client

    docs = [{'content': '原始', 'chunk_id': 1}]
    rerank_docs('query', docs, top_k=5)

    # 原 dict 不应被添加 rerank_score
    assert 'rerank_score' not in docs[0]
