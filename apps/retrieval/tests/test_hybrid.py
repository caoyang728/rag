"""
retrieval.hybrid 单元测试
覆盖：rrf_fuse 融合算法、hybrid_search 主流程（mock embedding/vector/bm25/rerank）
"""
import pytest
from unittest.mock import patch, MagicMock


class TestRrfFuse:
    """Reciprocal Rank Fusion 算法测试"""

    def test_empty_lists(self):
        from apps.retrieval.hybrid import rrf_fuse
        assert rrf_fuse() == []
        assert rrf_fuse([], []) == []

    def test_single_list(self):
        from apps.retrieval.hybrid import rrf_fuse
        items = [
            {'chunk_id': 1, 'score': 0.9},
            {'chunk_id': 2, 'score': 0.8},
            {'chunk_id': 3, 'score': 0.7},
        ]
        result = rrf_fuse(items, k=60, top_k=10)
        assert len(result) == 3
        assert result[0]['chunk_id'] == 1
        # RRF score should be 1/(60+1) for rank 1
        assert result[0]['rrf_score'] == pytest.approx(1.0 / 61, rel=1e-6)

    def test_two_lists_same_item(self):
        from apps.retrieval.hybrid import rrf_fuse
        list_a = [{'chunk_id': 1, 'score': 0.9}]
        list_b = [{'chunk_id': 1, 'score': 0.8}]
        result = rrf_fuse(list_a, list_b, k=60)
        assert len(result) == 1
        # Same item appears in both lists, scores summed
        expected = 1.0 / 61 + 1.0 / 61
        assert result[0]['rrf_score'] == pytest.approx(expected, rel=1e-6)
        # from 字段初始化为空列表（当前实现不追踪来源）
        assert isinstance(result[0]['from'], list)

    def test_two_lists_different_items(self):
        from apps.retrieval.hybrid import rrf_fuse
        list_a = [{'chunk_id': 1}]
        list_b = [{'chunk_id': 2}]
        result = rrf_fuse(list_a, list_b, k=60)
        assert len(result) == 2
        # Both should have score 1/61
        assert result[0]['rrf_score'] == result[1]['rrf_score']

    def test_top_k_truncation(self):
        from apps.retrieval.hybrid import rrf_fuse
        items_a = [{'chunk_id': i} for i in range(1, 20)]
        items_b = [{'chunk_id': i} for i in range(15, 35)]
        result = rrf_fuse(items_a, items_b, k=60, top_k=10)
        assert len(result) == 10

    def test_custom_k(self):
        from apps.retrieval.hybrid import rrf_fuse
        items = [{'chunk_id': 1}, {'chunk_id': 2}]
        result_k10 = rrf_fuse(items, k=10)
        result_k60 = rrf_fuse(items, k=60)
        # With k=10, rank 1 contributes 1/11 ≈ 0.0909
        assert result_k10[0]['rrf_score'] == pytest.approx(1.0 / 11, rel=1e-6)
        assert result_k60[0]['rrf_score'] == pytest.approx(1.0 / 61, rel=1e-6)

    def test_rrf_score_ordering(self):
        from apps.retrieval.hybrid import rrf_fuse
        items_a = [
            {'chunk_id': 1},  # rank 1
            {'chunk_id': 2},  # rank 2
            {'chunk_id': 3},  # rank 3
        ]
        items_b = [
            {'chunk_id': 2},  # rank 1
            {'chunk_id': 4},  # rank 2
        ]
        result = rrf_fuse(items_a, items_b, k=60)
        # chunk 2 appears in both lists (rank 2 in A, rank 1 in B), should have highest score
        assert result[0]['chunk_id'] == 2
        # chunk 1 appears only in A at rank 1
        assert result[1]['chunk_id'] == 1 or result[1]['chunk_id'] == 4


class TestHybridSearch:
    """hybrid_search 主流程测试（mock 外部依赖）"""

    @patch('apps.retrieval.hybrid._enrich_chunks')
    @patch('apps.retrieval.hybrid.rerank_docs')
    @patch('apps.retrieval.hybrid.rrf_fuse')
    @patch('apps.retrieval.hybrid.bm25_search')
    @patch('apps.retrieval.hybrid.vector_search')
    @patch('apps.retrieval.hybrid.get_embedding_client')
    def test_hybrid_search_success(self, mock_embed_cls, mock_vec, mock_bm,
                                   mock_rrf, mock_rerank, mock_enrich):
        """正常流程：embed → vector/bm25并行 → RRF → Rerank"""
        mock_embed = MagicMock()
        mock_embed.embed_one.return_value = [0.1, 0.2, 0.3]
        mock_embed_cls.return_value = mock_embed

        mock_vec.return_value = [
            {'chunk_id': 10, 'rrf_score': 0.5, 'document_id': 1},
            {'chunk_id': 11, 'rrf_score': 0.4, 'document_id': 2},
        ]
        mock_bm.return_value = [
            {'chunk_id': 10, 'rrf_score': 0.5, 'document_id': 1},
            {'chunk_id': 12, 'rrf_score': 0.3, 'document_id': 3},
        ]
        mock_rrf.return_value = [
            {'chunk_id': 10, 'rrf_score': 0.8, 'document_id': 1},
            {'chunk_id': 11, 'rrf_score': 0.4, 'document_id': 2},
            {'chunk_id': 12, 'rrf_score': 0.3, 'document_id': 3},
        ]
        mock_rerank.return_value = [
            {'chunk_id': 10, 'rrf_score': 0.8, 'rerank_score': 0.9, 'document_id': 1},
            {'chunk_id': 11, 'rrf_score': 0.4, 'rerank_score': 0.7, 'document_id': 2},
        ]

        from apps.retrieval.hybrid import hybrid_search
        user = MagicMock()
        result = hybrid_search('test query', user, do_rerank=True)

        mock_embed.embed_one.assert_called_once_with('test query')
        mock_rerank.assert_called_once()
        mock_enrich.assert_called_once()
        assert len(result['chunks']) == 2
        assert 'stats' in result
        assert 'vector_ms' in result['stats']
        assert 'total_ms' in result['stats']

    @patch('apps.retrieval.hybrid.get_embedding_client')
    def test_hybrid_search_embedding_failure(self, mock_embed_cls):
        """Embedding 异常应向上抛出"""
        mock_embed = MagicMock()
        mock_embed.embed_one.side_effect = Exception('Embedding failed')
        mock_embed_cls.return_value = mock_embed

        from apps.retrieval.hybrid import hybrid_search
        user = MagicMock()
        with pytest.raises(Exception, match='Embedding failed'):
            hybrid_search('test', user)

    @patch('apps.retrieval.hybrid._enrich_chunks')
    @patch('apps.retrieval.hybrid.rerank_docs')
    @patch('apps.retrieval.hybrid.rrf_fuse')
    @patch('apps.retrieval.hybrid.bm25_search')
    @patch('apps.retrieval.hybrid.vector_search')
    @patch('apps.retrieval.hybrid.get_embedding_client')
    def test_hybrid_search_no_rerank(self, mock_embed_cls, mock_vec, mock_bm,
                                      mock_rrf, mock_rerank, mock_enrich):
        """do_rerank=False 时不应调用 rerank_docs"""
        mock_embed = MagicMock()
        mock_embed.embed_one.return_value = [0.1]
        mock_embed_cls.return_value = mock_embed

        mock_vec.return_value = [{'chunk_id': 1, 'document_id': 1}]
        mock_bm.return_value = [{'chunk_id': 1, 'document_id': 1}]
        mock_rrf.return_value = [{'chunk_id': 1, 'document_id': 1}]

        from apps.retrieval.hybrid import hybrid_search
        user = MagicMock()
        result = hybrid_search('q', user, do_rerank=False)

        mock_rerank.assert_not_called()
        assert result['stats']['rerank_ms'] == 0

    @patch('apps.retrieval.hybrid._enrich_chunks')
    @patch('apps.retrieval.hybrid.rerank_docs')
    @patch('apps.retrieval.hybrid.rrf_fuse')
    @patch('apps.retrieval.hybrid.bm25_search')
    @patch('apps.retrieval.hybrid.vector_search')
    @patch('apps.retrieval.hybrid.get_embedding_client')
    def test_hybrid_search_empty_rrf_rerank_skipped(self, mock_embed_cls, mock_vec,
                                                    mock_bm, mock_rrf, mock_rerank, mock_enrich):
        """RRF 结果为空时，Rerank 应跳过"""
        mock_embed = MagicMock()
        mock_embed.embed_one.return_value = [0.1]
        mock_embed_cls.return_value = mock_embed

        mock_vec.return_value = []
        mock_bm.return_value = []
        mock_rrf.return_value = []

        from apps.retrieval.hybrid import hybrid_search
        user = MagicMock()
        result = hybrid_search('q', user, do_rerank=True)

        mock_rerank.assert_not_called()
        assert result['chunks'] == []

    @patch('apps.retrieval.hybrid._enrich_chunks')
    @patch('apps.retrieval.hybrid.rerank_docs')
    @patch('apps.retrieval.hybrid.rrf_fuse')
    @patch('apps.retrieval.hybrid.bm25_search')
    @patch('apps.retrieval.hybrid.vector_search')
    @patch('apps.retrieval.hybrid.get_embedding_client')
    def test_hybrid_search_custom_top_k(self, mock_embed_cls, mock_vec, mock_bm,
                                         mock_rrf, mock_rerank, mock_enrich):
        """自定义 vector_top_k / bm25_top_k"""
        mock_embed = MagicMock()
        mock_embed.embed_one.return_value = [0.1]
        mock_embed_cls.return_value = mock_embed

        mock_vec.return_value = [{'chunk_id': i, 'document_id': 1} for i in range(5)]
        mock_bm.return_value = [{'chunk_id': i, 'document_id': 1} for i in range(5)]
        mock_rrf.return_value = [{'chunk_id': i, 'document_id': 1} for i in range(5)]
        mock_rerank.return_value = [{'chunk_id': i, 'document_id': 1} for i in range(3)]

        from apps.retrieval.hybrid import hybrid_search
        user = MagicMock()
        hybrid_search('q', user, vector_top_k=20, bm25_top_k=15, do_rerank=True)

        mock_vec.assert_called_once()
        call_kwargs = mock_vec.call_args[1]
        assert call_kwargs['top_k'] == 20

        mock_bm.assert_called_once()
        bm_kwargs = mock_bm.call_args[1]
        assert bm_kwargs['top_k'] == 15

    @patch('apps.retrieval.hybrid._enrich_chunks')
    @patch('apps.retrieval.hybrid.rerank_docs')
    @patch('apps.retrieval.hybrid.rrf_fuse')
    @patch('apps.retrieval.hybrid.bm25_search')
    @patch('apps.retrieval.hybrid.vector_search')
    @patch('apps.retrieval.hybrid.get_embedding_client')
    def test_hybrid_search_stats_structure(self, mock_embed_cls, mock_vec, mock_bm,
                                           mock_rrf, mock_rerank, mock_enrich):
        """验证返回 stats 结构完整性"""
        mock_embed = MagicMock()
        mock_embed.embed_one.return_value = [0.1]
        mock_embed_cls.return_value = mock_embed

        mock_vec.return_value = [{'chunk_id': 1, 'document_id': 1}]
        mock_bm.return_value = [{'chunk_id': 2, 'document_id': 2}]
        mock_rrf.return_value = [{'chunk_id': 1, 'document_id': 1}]
        mock_rerank.return_value = [{'chunk_id': 1, 'document_id': 1}]

        from apps.retrieval.hybrid import hybrid_search
        user = MagicMock()
        result = hybrid_search('q', user)

        stats = result['stats']
        assert 'vector_ms' in stats
        assert 'bm25_ms' in stats
        assert 'rrf_ms' in stats
        assert 'rerank_ms' in stats
        assert 'total_ms' in stats
        assert all(isinstance(v, int) for v in stats.values())
