"""
apps.wiki.retriever 单元测试 —— Wiki 页面向量检索

覆盖范围：
- search_wiki：零向量兜底返回 []、空结果、正常命中、阈值过滤、top_k 截断、默认参数
- score 计算公式：score = max(0.0, 1.0 - distance)
- distance 为 None 时按 0.0 处理（score=1.0）
- 结果结构：{'wiki_id', 'title', 'summary', 'content', 'tags', 'score'} 且按 score 降序

纯 mock（不依赖 DB）：
search_wiki 调用 get_embedding_client 发起向量化、用 pgvector 的 CosineDistance
做向量检索，依赖外部 embedding 服务与 HNSW 索引。本测试聚焦分支逻辑
（零向量短路、阈值过滤、top_k 截断、score 换算），故统一 mock get_embedding_client
与 WikiPage.objects 查询链，避免真实网络与 DB 耦合。
"""
import pytest
from unittest.mock import patch, MagicMock

from apps.wiki.retriever import search_wiki


# ----------------------------------------------------------------------------
# 辅助：构造一个非零向量，绕过零向量短路分支
# ----------------------------------------------------------------------------
_NONZERO_VEC = [0.1, 0.2, 0.3]


def _mock_embed_client(vec=None):
    """构造 embedding 客户端 mock，embed_one 返回指定向量"""
    client = MagicMock()
    client.embed_one.return_value = vec if vec is not None else _NONZERO_VEC
    return client


def _mock_wiki_page_qs(rows):
    """配置 WikiPage.objects 查询链，使最终切片返回给定 rows 列表

    查询链：filter().annotate().order_by().values()[:top_k*2]
    切片调用 __getitem__(slice)，将其 return_value 设为 rows 列表，
    使 `for row in qs` 能直接迭代 rows。
    """
    mock_qs = MagicMock()
    mock_qs.__getitem__.return_value = rows  # 切片返回真实列表，可直接迭代
    mock_page = MagicMock()
    (mock_page.objects.filter.return_value
     .annotate.return_value
     .order_by.return_value
     .values.return_value) = mock_qs
    return mock_page


def _make_row(wiki_id=1, title='页面', summary='摘要', content='正文',
              tags=None, distance=0.1):
    """构造单条查询结果 row（values 返回的 dict 结构）"""
    return {
        'id': wiki_id,
        'title': title,
        'summary': summary,
        'content': content,
        'tags': tags if tags is not None else ['t1', 't2'],
        'distance': distance,
    }


# ============================================================================
# 零向量兜底：embedding 失败返回零向量时直接空结果
# ============================================================================
class TestSearchWikiZeroVector:
    """embed_one 返回零向量时应短路返回 []，不查询 DB"""

    @pytest.mark.unit
    @patch('apps.wiki.retriever.WikiPage')
    @patch('apps.wiki.retriever.get_embedding_client')
    def test_zero_vector_returns_empty(self, mock_get_client, mock_page):
        """全零向量时返回空列表，不触发后续向量检索"""
        mock_get_client.return_value = _mock_embed_client([0.0, 0.0, 0.0])
        # WikiPage 不应被查询
        result = search_wiki('问题')
        assert result == []
        mock_page.objects.filter.assert_not_called()

    @pytest.mark.unit
    @patch('apps.wiki.retriever.WikiPage')
    @patch('apps.wiki.retriever.get_embedding_client')
    def test_zero_vector_does_not_query_db(self, mock_get_client, mock_page):
        """全零向量短路：WikiPage.objects.filter 不应被调用，节省 DB 开销"""
        mock_get_client.return_value = _mock_embed_client([0.0] * 1024)
        search_wiki('任意问题')
        mock_page.objects.filter.assert_not_called()


# ============================================================================
# 调用 embedding：验证 embed_one 入参与查询链
# ============================================================================
class TestSearchWikiEmbeddingCall:
    """search_wiki 调用 embedding 客户端的契约测试"""

    @pytest.mark.unit
    @patch('apps.wiki.retriever.WikiPage')
    @patch('apps.wiki.retriever.get_embedding_client')
    def test_embed_one_called_with_query(self, mock_get_client, mock_page):
        """embed_one 应以原始 query 作为参数调用"""
        client = _mock_embed_client()
        mock_get_client.return_value = client
        mock_page.objects.filter.return_value.annotate.return_value.order_by.return_value \
            .values.return_value.__getitem__.return_value = []

        search_wiki('请假流程', top_k=3)

        client.embed_one.assert_called_once_with('请假流程')

    @pytest.mark.unit
    @patch('apps.wiki.retriever.WikiPage')
    @patch('apps.wiki.retriever.get_embedding_client')
    def test_query_chain_uses_published_filter(self, mock_get_client, mock_page):
        """查询链应以 status='published' 且 embedding 非空为前置过滤"""
        mock_get_client.return_value = _mock_embed_client()
        mock_page.objects.filter.return_value.annotate.return_value.order_by.return_value \
            .values.return_value.__getitem__.return_value = []

        search_wiki('问题')

        # filter 应被调用，且含 status='published' 关键字参数
        mock_page.objects.filter.assert_called_once()
        _, kwargs = mock_page.objects.filter.call_args
        assert kwargs.get('status') == 'published'
        assert 'embedding__isnull' in kwargs


# ============================================================================
# 空结果：DB 无命中
# ============================================================================
class TestSearchWikiNoResults:
    """查询返回空列表时的行为"""

    @pytest.mark.unit
    @patch('apps.wiki.retriever.WikiPage')
    @patch('apps.wiki.retriever.get_embedding_client')
    def test_empty_resultset_returns_empty(self, mock_get_client, mock_page):
        """DB 返回空列表时 search_wiki 应返回 []"""
        mock_get_client.return_value = _mock_embed_client()
        mock_page.objects.filter.return_value.annotate.return_value.order_by.return_value \
            .values.return_value.__getitem__.return_value = []

        assert search_wiki('不存在的问题') == []


# ============================================================================
# 正常命中：score 计算、阈值过滤、结果结构
# ============================================================================
class TestSearchWikiSuccess:
    """search_wiki 正常命中结果测试"""

    @pytest.mark.unit
    @patch('apps.wiki.retriever.WikiPage')
    @patch('apps.wiki.retriever.get_embedding_client')
    def test_success_returns_scored_results(self, mock_get_client, mock_page):
        """命中结果应包含 score（=1-distance）并按 distance 升序保持顺序"""
        mock_get_client.return_value = _mock_embed_client()
        rows = [
            _make_row(wiki_id=1, title='页面A', distance=0.1),  # score=0.9
            _make_row(wiki_id=2, title='页面B', distance=0.3),  # score=0.7
        ]
        mock_page.objects.filter.return_value.annotate.return_value.order_by.return_value \
            .values.return_value.__getitem__.return_value = rows

        result = search_wiki('问题', top_k=3, threshold=0.5)

        assert len(result) == 2
        assert result[0]['wiki_id'] == 1
        assert result[0]['score'] == 0.9
        assert result[1]['wiki_id'] == 2
        assert result[1]['score'] == 0.7

    @pytest.mark.unit
    @patch('apps.wiki.retriever.WikiPage')
    @patch('apps.wiki.retriever.get_embedding_client')
    def test_result_structure_keys(self, mock_get_client, mock_page):
        """每条结果应包含 wiki_id/title/summary/content/tags/score 六个字段"""
        mock_get_client.return_value = _mock_embed_client()
        rows = [_make_row(wiki_id=10, title='结构测试', summary='概要',
                          content='正文', tags=['a', 'b'], distance=0.2)]
        mock_page.objects.filter.return_value.annotate.return_value.order_by.return_value \
            .values.return_value.__getitem__.return_value = rows

        result = search_wiki('问题', top_k=3, threshold=0.5)

        assert set(result[0].keys()) == {
            'wiki_id', 'title', 'summary', 'content', 'tags', 'score'}
        assert result[0]['wiki_id'] == 10
        assert result[0]['title'] == '结构测试'
        assert result[0]['tags'] == ['a', 'b']

    @pytest.mark.unit
    @patch('apps.wiki.retriever.WikiPage')
    @patch('apps.wiki.retriever.get_embedding_client')
    def test_score_rounded_to_4_decimals(self, mock_get_client, mock_page):
        """score 应 round 到 4 位小数，避免浮点精度噪声"""
        mock_get_client.return_value = _mock_embed_client()
        # distance=0.123456 -> score=0.876544 -> round -> 0.8765
        rows = [_make_row(distance=0.123456)]
        mock_page.objects.filter.return_value.annotate.return_value.order_by.return_value \
            .values.return_value.__getitem__.return_value = rows

        result = search_wiki('问题', top_k=3, threshold=0.5)
        assert result[0]['score'] == 0.8765


# ============================================================================
# 阈值过滤：低于 threshold 的结果被剔除
# ============================================================================
class TestSearchWikiThreshold:
    """threshold 阈值过滤测试"""

    @pytest.mark.unit
    @patch('apps.wiki.retriever.WikiPage')
    @patch('apps.wiki.retriever.get_embedding_client')
    def test_below_threshold_excluded(self, mock_get_client, mock_page):
        """score < threshold 的结果应被剔除"""
        mock_get_client.return_value = _mock_embed_client()
        rows = [
            _make_row(wiki_id=1, distance=0.1),   # score=0.9 命中
            _make_row(wiki_id=2, distance=0.5),   # score=0.5 低于阈值
        ]
        mock_page.objects.filter.return_value.annotate.return_value.order_by.return_value \
            .values.return_value.__getitem__.return_value = rows

        result = search_wiki('问题', top_k=3, threshold=0.6)

        assert len(result) == 1
        assert result[0]['wiki_id'] == 1

    @pytest.mark.unit
    @patch('apps.wiki.retriever.WikiPage')
    @patch('apps.wiki.retriever.get_embedding_client')
    def test_default_threshold_078(self, mock_get_client, mock_page):
        """未传 threshold 时默认 0.78，score>=0.78 才返回"""
        mock_get_client.return_value = _mock_embed_client()
        rows = [
            _make_row(wiki_id=1, distance=0.22),   # score=0.78 命中（>=）
            _make_row(wiki_id=2, distance=0.221),  # score=0.779 剔除
        ]
        mock_page.objects.filter.return_value.annotate.return_value.order_by.return_value \
            .values.return_value.__getitem__.return_value = rows

        result = search_wiki('问题')  # 使用默认 threshold=0.78
        assert len(result) == 1
        assert result[0]['wiki_id'] == 1

    @pytest.mark.unit
    @patch('apps.wiki.retriever.WikiPage')
    @patch('apps.wiki.retriever.get_embedding_client')
    def test_all_below_threshold_returns_empty(self, mock_get_client, mock_page):
        """全部低于阈值时返回空列表"""
        mock_get_client.return_value = _mock_embed_client()
        rows = [_make_row(distance=0.95)]  # score=0.05
        mock_page.objects.filter.return_value.annotate.return_value.order_by.return_value \
            .values.return_value.__getitem__.return_value = rows

        assert search_wiki('问题', threshold=0.78) == []


# ============================================================================
# top_k 截断
# ============================================================================
class TestSearchWikiTopK:
    """top_k 截断测试"""

    @pytest.mark.unit
    @patch('apps.wiki.retriever.WikiPage')
    @patch('apps.wiki.retriever.get_embedding_client')
    def test_top_k_limits_results(self, mock_get_client, mock_page):
        """命中数超过 top_k 时只返回前 top_k 条"""
        mock_get_client.return_value = _mock_embed_client()
        rows = [
            _make_row(wiki_id=i, distance=0.1 * i) for i in range(5)
        ]
        mock_page.objects.filter.return_value.annotate.return_value.order_by.return_value \
            .values.return_value.__getitem__.return_value = rows

        result = search_wiki('问题', top_k=2, threshold=0.0)
        assert len(result) == 2
        # 保留前两条（已按 distance 升序，即 score 降序）
        assert result[0]['wiki_id'] == 0
        assert result[1]['wiki_id'] == 1

    @pytest.mark.unit
    @patch('apps.wiki.retriever.WikiPage')
    @patch('apps.wiki.retriever.get_embedding_client')
    def test_default_top_k_is_3(self, mock_get_client, mock_page):
        """未传 top_k 时默认返回至多 3 条"""
        mock_get_client.return_value = _mock_embed_client()
        rows = [_make_row(wiki_id=i, distance=0.01 * i) for i in range(5)]
        mock_page.objects.filter.return_value.annotate.return_value.order_by.return_value \
            .values.return_value.__getitem__.return_value = rows

        result = search_wiki('问题', threshold=0.0)
        assert len(result) == 3


# ============================================================================
# distance 边界：None / 负值 / 超大值
# ============================================================================
class TestSearchWikiDistanceEdge:
    """distance 字段边界值处理测试"""

    @pytest.mark.unit
    @patch('apps.wiki.retriever.WikiPage')
    @patch('apps.wiki.retriever.get_embedding_client')
    def test_distance_none_treated_as_zero(self, mock_get_client, mock_page):
        """distance 为 None 时 `row['distance'] or 0.0` 取 0.0，score=1.0"""
        mock_get_client.return_value = _mock_embed_client()
        rows = [_make_row(distance=None)]
        mock_page.objects.filter.return_value.annotate.return_value.order_by.return_value \
            .values.return_value.__getitem__.return_value = rows

        result = search_wiki('问题', threshold=0.78)
        assert result[0]['score'] == 1.0

    @pytest.mark.unit
    @patch('apps.wiki.retriever.WikiPage')
    @patch('apps.wiki.retriever.get_embedding_client')
    def test_distance_large_clamps_score_to_zero(self, mock_get_client, mock_page):
        """distance>1 时 1-distance 为负，max(0.0, ...) 钳为 0.0，低于阈值被剔除"""
        mock_get_client.return_value = _mock_embed_client()
        rows = [_make_row(distance=2.0)]  # 1-2=-1 -> max(0,-1)=0
        mock_page.objects.filter.return_value.annotate.return_value.order_by.return_value \
            .values.return_value.__getitem__.return_value = rows

        # score=0.0 < 0.78 阈值，应被剔除
        assert search_wiki('问题', threshold=0.78) == []
