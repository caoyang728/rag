"""
apps.retrieval.bm25 单元测试 —— BM25 关键词检索

覆盖范围：
- tokenize：jieba 分词 + 短词过滤（<2 字符剔除）
- bm25_search：空查询/无候选/正常打分/归一化/top_k 截断/keyword_weight 加权

用纯 mock（不依赖 DB）：
bm25_search 内部组合了 build_permission_q、DocumentVector 查询、BM25Okapi 打分、
KeywordWeight 加权四段逻辑，逐段 mock 后可独立验证每条分支，避免 DB 数据污染。
tokenize 是无状态纯函数，直接调用真实 jieba 即可。
"""
import pytest
from unittest.mock import patch, MagicMock


# ============================================================================
# tokenize：jieba 分词 + 短词过滤
# ============================================================================
@pytest.mark.unit
def test_tokenize_empty():
    """空字符串应返回空列表"""
    from apps.retrieval.bm25 import tokenize
    assert tokenize('') == []


@pytest.mark.unit
def test_tokenize_none():
    """None 输入应返回空列表（not text 短路）"""
    from apps.retrieval.bm25 import tokenize
    assert tokenize(None) == []


@pytest.mark.unit
def test_tokenize_short_filtered():
    """单字符（<2 字符）应被过滤掉"""
    from apps.retrieval.bm25 import tokenize
    # 'a' 长度为 1，过滤后应返回空列表
    result = tokenize('a')
    assert result == []


@pytest.mark.unit
def test_tokenize_normal():
    """正常中文分词应返回非空 token 列表，且每个 token 长度 >= 2"""
    from apps.retrieval.bm25 import tokenize
    result = tokenize('你好世界')
    assert isinstance(result, list)
    assert len(result) > 0
    # 所有 token 去空白后长度 >= 2（短词过滤铁律）
    assert all(len(t.strip()) >= 2 for t in result)


# ============================================================================
# bm25_search：空查询 / 无 token / 无候选
# ============================================================================
@pytest.mark.unit
@patch('apps.retrieval.bm25.KeywordWeight')
@patch('apps.retrieval.bm25.DocumentVector')
@patch('apps.retrieval.bm25.build_permission_q')
def test_bm25_search_empty_query(mock_perm_q, mock_dv, mock_kw):
    """空查询应直接返回空列表，不触发权限构建和 DB 查询"""
    from apps.retrieval.bm25 import bm25_search
    user = MagicMock()
    result = bm25_search('', user)
    assert result == []
    # 空查询短路返回，不应调用权限构建
    mock_perm_q.assert_not_called()
    mock_dv.objects.filter.assert_not_called()


@pytest.mark.unit
@patch('apps.retrieval.bm25.KeywordWeight')
@patch('apps.retrieval.bm25.DocumentVector')
@patch('apps.retrieval.bm25.build_permission_q')
def test_bm25_search_no_tokens(mock_perm_q, mock_dv, mock_kw):
    """查询分词后无 token（全部为短词）应返回空列表"""
    from apps.retrieval.bm25 import bm25_search
    user = MagicMock()
    # 'a' 分词后为 ['a']，过滤后为 []，触发无 token 短路
    result = bm25_search('a', user)
    assert result == []
    mock_perm_q.assert_not_called()


@pytest.mark.unit
@patch('apps.retrieval.bm25.KeywordWeight')
@patch('apps.retrieval.bm25.BM25Okapi')
@patch('apps.retrieval.bm25.DocumentVector')
@patch('apps.retrieval.bm25.build_permission_q')
def test_bm25_search_no_candidates(mock_perm_q, mock_dv, mock_bm25_cls, mock_kw):
    """候选池为空时应返回空列表，不进入打分阶段"""
    from apps.retrieval.bm25 import bm25_search
    # 构造空候选池：DocumentVector 查询链最终返回空列表
    mock_qs = MagicMock()
    mock_dv.objects.filter.return_value = mock_qs
    mock_qs.filter.return_value.values.return_value.__getitem__.return_value = []

    user = MagicMock()
    result = bm25_search('测试查询', user)
    assert result == []
    # 无候选时不应实例化 BM25Okapi
    mock_bm25_cls.assert_not_called()


# ============================================================================
# bm25_search：正常打分 / 归一化 / top_k 截断
# ============================================================================
def _make_candidate(cid, content_preview, keywords=None):
    """构造单个候选 dict，对齐 DocumentVector.values() 的字段结构"""
    return {
        'id': cid, 'chunk_id': cid * 10, 'document_id': cid * 100,
        'node_id': cid * 1000, 'visibility_level': 'TEAM_ONLY',
        'root_type': 'kb_default', 'node_path': '/',
        'content_preview': content_preview, 'chunk_type': 'text',
        'keywords': keywords or [],
    }


@pytest.mark.unit
@patch('apps.retrieval.bm25.KeywordWeight')
@patch('apps.retrieval.bm25.BM25Okapi')
@patch('apps.retrieval.bm25.DocumentVector')
@patch('apps.retrieval.bm25.build_permission_q')
def test_bm25_search_with_results(mock_perm_q, mock_dv, mock_bm25_cls, mock_kw):
    """有候选时应返回打分结果，结构包含 vector_id/score 等字段"""
    from apps.retrieval.bm25 import bm25_search
    candidates = [
        _make_candidate(1, '苹果是一种水果', ['苹果', '水果']),
        _make_candidate(2, '香蕉也是水果', ['香蕉', '水果']),
    ]
    mock_qs = MagicMock()
    mock_dv.objects.filter.return_value = mock_qs
    mock_qs.filter.return_value.values.return_value.__getitem__.return_value = candidates

    # 模拟 BM25 打分：candidate 1 分数更高
    mock_bm25 = MagicMock()
    mock_bm25_cls.return_value = mock_bm25
    mock_bm25.get_scores.return_value = [1.5, 0.5]

    # KeywordWeight 无命中，kw_bonus=1.0
    mock_kw.objects.filter.return_value = []

    user = MagicMock()
    result = bm25_search('苹果', user)

    assert len(result) == 2
    # 分数高的排在前面
    assert result[0]['vector_id'] == 1
    assert result[1]['vector_id'] == 2
    # 字段完整性
    assert 'score' in result[0]
    assert 'chunk_id' in result[0]
    assert 'document_id' in result[0]
    assert 'content' in result[0]
    assert 'node_path' in result[0]


@pytest.mark.unit
@patch('apps.retrieval.bm25.KeywordWeight')
@patch('apps.retrieval.bm25.BM25Okapi')
@patch('apps.retrieval.bm25.DocumentVector')
@patch('apps.retrieval.bm25.build_permission_q')
def test_bm25_search_normalization(mock_perm_q, mock_dv, mock_bm25_cls, mock_kw):
    """打分结果应归一化到 [0,1]，最高分归一为 1.0"""
    from apps.retrieval.bm25 import bm25_search
    candidates = [
        _make_candidate(1, '苹果是一种水果'),
        _make_candidate(2, '香蕉也是水果'),
    ]
    mock_qs = MagicMock()
    mock_dv.objects.filter.return_value = mock_qs
    mock_qs.filter.return_value.values.return_value.__getitem__.return_value = candidates

    mock_bm25 = MagicMock()
    mock_bm25_cls.return_value = mock_bm25
    # 原始分数 [3.0, 1.5]，归一化后 [1.0, 0.5]
    mock_bm25.get_scores.return_value = [3.0, 1.5]
    mock_kw.objects.filter.return_value = []

    user = MagicMock()
    result = bm25_search('苹果', user)

    # 最高分归一为 1.0
    assert result[0]['score'] == pytest.approx(1.0, rel=1e-6)
    # 次高分 = 1.5 / 3.0 = 0.5
    assert result[1]['score'] == pytest.approx(0.5, rel=1e-6)
    # 所有分数在 [0, 1] 区间内
    assert all(0.0 <= x['score'] <= 1.0 for x in result)


@pytest.mark.unit
@patch('apps.retrieval.bm25.KeywordWeight')
@patch('apps.retrieval.bm25.BM25Okapi')
@patch('apps.retrieval.bm25.DocumentVector')
@patch('apps.retrieval.bm25.build_permission_q')
def test_bm25_search_top_k(mock_perm_q, mock_dv, mock_bm25_cls, mock_kw):
    """结果应限制在 top_k 内，返回前 top_k 条"""
    from apps.retrieval.bm25 import bm25_search
    # 构造 5 个候选
    candidates = [_make_candidate(i, f'内容{i} 苹果') for i in range(1, 6)]
    mock_qs = MagicMock()
    mock_dv.objects.filter.return_value = mock_qs
    mock_qs.filter.return_value.values.return_value.__getitem__.return_value = candidates

    mock_bm25 = MagicMock()
    mock_bm25_cls.return_value = mock_bm25
    # 分数递减，确保排序后取前 3
    mock_bm25.get_scores.return_value = [5.0, 4.0, 3.0, 2.0, 1.0]
    mock_kw.objects.filter.return_value = []

    user = MagicMock()
    result = bm25_search('苹果', user, top_k=3)

    assert len(result) == 3
    # 取分数最高的 3 个
    assert result[0]['vector_id'] == 1
    assert result[2]['vector_id'] == 3


@pytest.mark.unit
@patch('apps.retrieval.bm25.KeywordWeight')
@patch('apps.retrieval.bm25.BM25Okapi')
@patch('apps.retrieval.bm25.DocumentVector')
@patch('apps.retrieval.bm25.build_permission_q')
def test_bm25_search_keyword_weight(mock_perm_q, mock_dv, mock_bm25_cls, mock_kw):
    """KeywordWeight 命中时应按平均权重加成，验证 bonus 计算逻辑"""
    from apps.retrieval.bm25 import bm25_search
    candidates = [
        _make_candidate(1, '苹果是一种水果'),
        _make_candidate(2, '香蕉也是水果'),
    ]
    mock_qs = MagicMock()
    mock_dv.objects.filter.return_value = mock_qs
    mock_qs.filter.return_value.values.return_value.__getitem__.return_value = candidates

    mock_bm25 = MagicMock()
    mock_bm25_cls.return_value = mock_bm25
    # 原始分数 [1.0, 2.0]
    mock_bm25.get_scores.return_value = [1.0, 2.0]

    # 构造 KeywordWeight：苹果 权重 2.0
    mock_kw_obj = MagicMock()
    mock_kw_obj.keyword = '苹果'
    mock_kw_obj.weight_score = 2.0
    mock_kw.objects.filter.return_value = [mock_kw_obj]

    user = MagicMock()
    result = bm25_search('苹果', user)

    # kw_bonus = 2.0 / 1 = 2.0
    # 加成后分数 [2.0, 4.0]
    # 归一化后 [0.5, 1.0]
    assert result[0]['vector_id'] == 2  # candidate 2 分数更高
    assert result[0]['score'] == pytest.approx(1.0, rel=1e-6)
    assert result[1]['vector_id'] == 1
    assert result[1]['score'] == pytest.approx(0.5, rel=1e-6)
    # 验证 KeywordWeight 查询使用了正确的 token
    mock_kw.objects.filter.assert_called_once()
    call_kwargs = mock_kw.objects.filter.call_args[1]
    assert 'keyword__in' in call_kwargs
    assert '苹果' in call_kwargs['keyword__in']
