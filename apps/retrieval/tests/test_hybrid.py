"""
retrieval.hybrid 单元测试
覆盖：rrf_fuse 融合算法、hybrid_search 主流程（mock embedding/vector/bm25/rerank）
"""
import pytest
from unittest.mock import patch, MagicMock

from apps.llm.embedding import EmbeddingException
from apps.knowledge.models import KnowledgeNode, Document, DocumentChunk, ImageResource
from apps.users.models import User


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

    @patch('apps.retrieval.hybrid.get_config_value')
    @patch('apps.retrieval.hybrid._enrich_chunks')
    @patch('apps.retrieval.hybrid.rerank_docs')
    @patch('apps.retrieval.hybrid.rrf_fuse')
    @patch('apps.retrieval.hybrid.bm25_search')
    @patch('apps.retrieval.hybrid.vector_search')
    @patch('apps.retrieval.hybrid.get_embedding_client')
    def test_hybrid_search_when_system_config_top_k_then_priority(
            self, mock_embed_cls, mock_vec, mock_bm, mock_rrf, mock_rerank,
            mock_enrich, mock_cfg):
        """未显式传 top_k 时，SystemConfig 的 VECTOR_TOP_K / BM25_TOP_K 优先于 settings"""
        mock_cfg.side_effect = lambda key, default=None, value_type=None: {
            'VECTOR_TOP_K': 42, 'BM25_TOP_K': 21,
        }.get(key, default)
        mock_embed = MagicMock()
        mock_embed.embed_one.return_value = [0.1]
        mock_embed_cls.return_value = mock_embed
        mock_vec.return_value = [{'chunk_id': i, 'document_id': 1} for i in range(5)]
        mock_bm.return_value = [{'chunk_id': i, 'document_id': 1} for i in range(5)]
        mock_rrf.return_value = [{'chunk_id': i, 'document_id': 1} for i in range(5)]
        mock_rerank.return_value = [{'chunk_id': i, 'document_id': 1} for i in range(3)]

        from apps.retrieval.hybrid import hybrid_search
        user = MagicMock()
        hybrid_search('q', user, do_rerank=True)

        assert mock_vec.call_args[1]['top_k'] == 42
        assert mock_bm.call_args[1]['top_k'] == 21

    @patch('apps.retrieval.hybrid.get_config_value')
    @patch('apps.retrieval.hybrid._enrich_chunks')
    @patch('apps.retrieval.hybrid.rerank_docs')
    @patch('apps.retrieval.hybrid.rrf_fuse')
    @patch('apps.retrieval.hybrid.bm25_search')
    @patch('apps.retrieval.hybrid.vector_search')
    @patch('apps.retrieval.hybrid.get_embedding_client')
    def test_hybrid_search_when_system_config_missing_then_settings_fallback(
            self, mock_embed_cls, mock_vec, mock_bm, mock_rrf, mock_rerank,
            mock_enrich, mock_cfg):
        """SystemConfig 未配置时回退 settings（.env）默认值，与旧部署行为一致"""
        mock_cfg.return_value = None
        mock_embed = MagicMock()
        mock_embed.embed_one.return_value = [0.1]
        mock_embed_cls.return_value = mock_embed
        mock_vec.return_value = [{'chunk_id': i, 'document_id': 1} for i in range(5)]
        mock_bm.return_value = [{'chunk_id': i, 'document_id': 1} for i in range(5)]
        mock_rrf.return_value = [{'chunk_id': i, 'document_id': 1} for i in range(5)]
        mock_rerank.return_value = [{'chunk_id': i, 'document_id': 1} for i in range(3)]

        from django.conf import settings
        from apps.retrieval.hybrid import hybrid_search
        user = MagicMock()
        hybrid_search('q', user, do_rerank=True)

        assert mock_vec.call_args[1]['top_k'] == settings.VECTOR_TOP_K
        assert mock_bm.call_args[1]['top_k'] == settings.BM25_TOP_K

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

    @patch('apps.retrieval.hybrid._enrich_chunks')
    @patch('apps.retrieval.hybrid.rerank_docs')
    @patch('apps.retrieval.hybrid.rrf_fuse')
    @patch('apps.retrieval.hybrid.bm25_search')
    @patch('apps.retrieval.hybrid.vector_search')
    @patch('apps.retrieval.hybrid.get_embedding_client')
    def test_hybrid_search_when_rerank_score_below_threshold_then_filtered(
            self, mock_embed_cls, mock_vec, mock_bm, mock_rrf, mock_rerank, mock_enrich):
        """rerank 分数低于阈值的片段应被过滤，避免无关文档作为引用返回"""
        mock_embed = MagicMock()
        mock_embed.embed_one.return_value = [0.1]
        mock_embed_cls.return_value = mock_embed
        mock_vec.return_value = [{'chunk_id': 1, 'document_id': 1}]
        mock_bm.return_value = [{'chunk_id': 1, 'document_id': 1}]
        mock_rrf.return_value = [{'chunk_id': 1, 'document_id': 1}]
        # 高相关 + 低相关（法律类无关文档）混合返回
        mock_rerank.return_value = [
            {'chunk_id': 1, 'rerank_score': 0.9, 'document_id': 1},
            {'chunk_id': 2, 'rerank_score': 0.1, 'document_id': 2},
        ]

        from apps.retrieval.hybrid import hybrid_search
        user = MagicMock()
        with patch('apps.retrieval.hybrid.settings.RETRIEVAL_MIN_RERANK_SCORE', 0.3):
            result = hybrid_search('q', user, do_rerank=True)

        assert result['chunks'] == [{'chunk_id': 1, 'rerank_score': 0.9, 'document_id': 1}]

    @patch('apps.retrieval.hybrid._enrich_chunks')
    @patch('apps.retrieval.hybrid.rerank_docs')
    @patch('apps.retrieval.hybrid.rrf_fuse')
    @patch('apps.retrieval.hybrid.bm25_search')
    @patch('apps.retrieval.hybrid.vector_search')
    @patch('apps.retrieval.hybrid.get_embedding_client')
    def test_hybrid_search_when_all_rerank_scores_below_threshold_then_empty(
            self, mock_embed_cls, mock_vec, mock_bm, mock_rrf, mock_rerank, mock_enrich):
        """所有 rerank 分数均低于阈值时返回空结果（触发上游拒答语义）"""
        mock_embed = MagicMock()
        mock_embed.embed_one.return_value = [0.1]
        mock_embed_cls.return_value = mock_embed
        mock_vec.return_value = [{'chunk_id': 1, 'document_id': 1}]
        mock_bm.return_value = [{'chunk_id': 1, 'document_id': 1}]
        mock_rrf.return_value = [{'chunk_id': 1, 'document_id': 1}]
        mock_rerank.return_value = [
            {'chunk_id': 1, 'rerank_score': 0.05, 'document_id': 1},
        ]

        from apps.retrieval.hybrid import hybrid_search
        user = MagicMock()
        with patch('apps.retrieval.hybrid.settings.RETRIEVAL_MIN_RERANK_SCORE', 0.3):
            result = hybrid_search('q', user, do_rerank=True)

        assert result['chunks'] == []

    @patch('apps.retrieval.hybrid._enrich_chunks')
    @patch('apps.retrieval.hybrid.rerank_docs')
    @patch('apps.retrieval.hybrid.rrf_fuse')
    @patch('apps.retrieval.hybrid.bm25_search')
    @patch('apps.retrieval.hybrid.vector_search')
    @patch('apps.retrieval.hybrid.get_embedding_client')
    def test_hybrid_search_when_rerank_score_missing_then_not_filtered(
            self, mock_embed_cls, mock_vec, mock_bm, mock_rrf, mock_rerank, mock_enrich):
        """rerank 失败回退（结果无 rerank_score 字段）时不触发阈值过滤，保持原行为"""
        mock_embed = MagicMock()
        mock_embed.embed_one.return_value = [0.1]
        mock_embed_cls.return_value = mock_embed
        mock_vec.return_value = [{'chunk_id': 1, 'document_id': 1}]
        mock_bm.return_value = [{'chunk_id': 1, 'document_id': 1}]
        mock_rrf.return_value = [{'chunk_id': 1, 'document_id': 1}]
        mock_rerank.return_value = [{'chunk_id': 1, 'document_id': 1}]

        from apps.retrieval.hybrid import hybrid_search
        user = MagicMock()
        with patch('apps.retrieval.hybrid.settings.RETRIEVAL_MIN_RERANK_SCORE', 0.3):
            result = hybrid_search('q', user, do_rerank=True)

        assert len(result['chunks']) == 1


# ============================================================================
# hybrid_search 异常分支（Embedding 失败 / 零向量）
# ============================================================================
@patch('apps.retrieval.hybrid.logger')
class TestHybridSearchEmbeddingErrors:
    """query embedding 失败与零向量防护测试"""

    @patch('apps.retrieval.hybrid.get_embedding_client')
    def test_embedding_exception_reraises(self, mock_embed_cls, mock_logger):
        """EmbeddingException 被捕获记录后向上重抛"""
        mock_embed = MagicMock()
        mock_embed.embed_one.side_effect = EmbeddingException('服务不可用')
        mock_embed_cls.return_value = mock_embed

        from apps.retrieval.hybrid import hybrid_search
        with pytest.raises(EmbeddingException, match='服务不可用'):
            hybrid_search('q', MagicMock())

    @patch('apps.retrieval.hybrid.get_embedding_client')
    def test_zero_vector_raises_embedding_exception(self, mock_embed_cls, mock_logger):
        """embedding 返回零向量时拒绝检索"""
        mock_embed = MagicMock()
        mock_embed.embed_one.return_value = [0.0, 0.0]
        mock_embed_cls.return_value = mock_embed

        from apps.retrieval.hybrid import hybrid_search
        with pytest.raises(EmbeddingException, match='零向量'):
            hybrid_search('q', MagicMock())


# ============================================================================
# _enrich_chunks（DB 集成：元信息补全 / 图片数据注入 / 未知文档兜底）
# ============================================================================
@pytest.mark.django_db
@pytest.mark.integration
class TestEnrichChunks:
    """_enrich_chunks 元信息补全测试（真实 DB）"""

    def _make_env(self):
        """构造：用户 → 节点 → 文档 → 切片，返回 (chunk_id, doc_id)"""
        user = User.objects.create_user(
            username='hybrid_user', password='x', email='hybrid@test.com')
        node = KnowledgeNode.objects.create(
            name='混合检索节点', root_type='company_doc', node_type='folder',
            node_kind='FOLDER', node_level=4, depth=0, path='/0001/',
            created_by=user)
        doc = Document.objects.create(
            node=node, title='测试文档', file_name='t.txt', file_type='txt',
            file_size=10, file_hash='h1', file_path='/tmp/t.txt',
            mime_type='text/plain', owner=user, dept_id=1,
            visibility_level='PUBLIC', status='done')
        return user, doc

    def test_enrich_fills_metadata(self):
        """chunk 元信息（section_path/page_number/content/extra/chunk_type）应补全"""
        user, doc = self._make_env()
        chunk = DocumentChunk.objects.create(
            document=doc, chunk_index=0, chunk_type='text', content='完整内容',
            section_path='章节一', page_number=2, extra={'group': 1})

        chunks = [{'chunk_id': chunk.id, 'document_id': doc.id, 'content': 'preview'}]
        from apps.retrieval.hybrid import _enrich_chunks
        _enrich_chunks(chunks)

        c = chunks[0]
        assert c['doc_title'] == '测试文档'
        assert c['section_path'] == '章节一'
        assert c['page_number'] == 2
        assert c['content'] == '完整内容'
        assert c['extra'] == {'group': 1}
        assert c['chunk_type'] == 'text'

    def test_enrich_unknown_document_title_fallback(self):
        """文档不存在时 doc_title 兜底为「未知文档」"""
        user, doc = self._make_env()
        chunk = DocumentChunk.objects.create(
            document=doc, chunk_index=0, chunk_type='text', content='c')

        chunks = [{'chunk_id': chunk.id, 'document_id': 999999}]
        from apps.retrieval.hybrid import _enrich_chunks
        _enrich_chunks(chunks)

        assert chunks[0]['doc_title'] == '未知文档'

    def test_enrich_image_data_injected(self):
        """chunk 带 image_id 时图片 base64/尺寸信息应注入 extra"""
        user, doc = self._make_env()
        img = ImageResource.objects.create(
            base64_data='base64xxx', width=100, height=50, mime_type='image/png')
        chunk = DocumentChunk.objects.create(
            document=doc, chunk_index=0, chunk_type='image', content='',
            image_id=img.id, extra={})

        chunks = [{'chunk_id': chunk.id, 'document_id': doc.id, 'extra': {}}]
        from apps.retrieval.hybrid import _enrich_chunks
        _enrich_chunks(chunks)

        extra = chunks[0]['extra']
        assert extra['base64_data'] == 'base64xxx'
        assert extra['width'] == 100
        assert extra['height'] == 50
        assert extra['mime_type'] == 'image/png'

    def test_enrich_empty_list_noop(self):
        """空列表不应报错"""
        from apps.retrieval.hybrid import _enrich_chunks
        _enrich_chunks([])
