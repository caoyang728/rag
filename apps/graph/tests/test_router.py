"""
graph.router 单元测试
覆盖：decide_route 三层路由决策逻辑（Wiki → GraphRAG → RAG 兜底）
使用 mock 在源模块级别替换 search_wiki / graphrag_search / hybrid_search
"""
import pytest
from unittest.mock import patch, MagicMock


def _patch_embed(mock_embed):
    """让 decide_route 内部 get_embedding_client().embed_one 返回非零向量，避免走 None 分支"""
    fake_vec = [0.1, 0.2, 0.3]
    mock_client = MagicMock()
    mock_client.embed_one.return_value = fake_vec
    mock_embed.return_value = mock_client


class TestDecideRouteWiki:
    """第 1 层：Wiki 快速命中"""

    @patch('apps.llm.embedding.get_embedding_client')
    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_wiki_direct_hit(self, mock_hybrid, mock_graph, mock_wiki, mock_embed):
        """Wiki 直接命中：score >= 0.68 时应返回 source='wiki'"""
        _patch_embed(mock_embed)
        mock_wiki.return_value = [
            {'title': '公司请假制度', 'content': '员工可请假', 'score': 0.85}
        ]
        mock_graph.return_value = {
            'source': 'graphrag_local', 'context': '', 'confidence': 0.10,
            'entities': [], 'relations': [], 'communities': [],
        }
        # 并行版：RAG 兜底层先 hybrid_search(rerank=true)，Wiki 命中后
        # _citation_chunks 再调一次 hybrid_search(rerank=false) 补充引用
        def _hybrid_side(*a, **kw):
            return {
                'chunks': [{'doc_title': '请假制度文档', 'section_path': 'S1',
                            'content': 'xx', 'chunk_id': 1, 'document_id': 1}],
                'stats': {},
            }
        mock_hybrid.side_effect = _hybrid_side
        user = MagicMock()
        from apps.graph.router import decide_route
        result = decide_route('请假流程', user)

        assert result['source'] == 'wiki'
        assert result['confidence'] == 0.85
        assert '公司请假制度' in result['context']
        # 并行版：三路都跑，route_trace 3 层
        assert len(result['route_trace']) == 3
        assert result['route_trace'][0]['layer'] == 'wiki'
        assert result['route_trace'][1]['layer'] == 'graphrag'
        assert result['route_trace'][2]['layer'] == 'rag'
        # GraphRAG 并行被调用一次（不提前返回）
        mock_graph.assert_called_once()
        # hybrid_search 被调用 2 次：RAG 兜底层 1 次 + citation_chunks 补充 1 次
        assert mock_hybrid.call_count == 2
        assert len(result['chunks']) == 1

    @patch('apps.llm.embedding.get_embedding_client')
    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_wiki_not_hit_graphrag_succeeds(self, mock_hybrid, mock_graph, mock_wiki, mock_embed):
        """Wiki 未命中(score<0.68)，GraphRAG 命中"""
        _patch_embed(mock_embed)
        mock_wiki.return_value = [
            {'title': '无关页面', 'content': '内容', 'score': 0.30}
        ]
        mock_graph.return_value = {
            'source': 'graphrag_local',
            'context': 'GraphRAG 上下文',
            'confidence': 0.60,
            'entities': ['张三'],
            'relations': [('张三', '负责', 'HR')],
            'communities': [],
        }
        # 并行版：RAG 兜底层先 hybrid_search，GraphRAG 命中后 _citation_chunks 再调一次
        def _hybrid_side(*a, **kw):
            return {
                'chunks': [{'doc_title': '张三档案', 'section_path': 'S1',
                            'content': 'xx', 'chunk_id': 2, 'document_id': 2}],
                'stats': {},
            }
        mock_hybrid.side_effect = _hybrid_side
        user = MagicMock()
        from apps.graph.router import decide_route
        result = decide_route('张三负责什么', user)

        assert result['source'] == 'graphrag_local'
        assert result['confidence'] == 0.60
        assert result['route_trace'][1]['layer'] == 'graphrag'
        # hybrid_search 被调用 2 次：RAG 兜底层 1 次 + citation_chunks 补充 1 次
        assert mock_hybrid.call_count == 2
        assert len(result['chunks']) == 1

    @patch('apps.llm.embedding.get_embedding_client')
    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_fallback_to_rag(self, mock_hybrid, mock_graph, mock_wiki, mock_embed):
        """Wiki 和 GraphRAG 均未命中，回退到 RAG"""
        _patch_embed(mock_embed)
        mock_wiki.return_value = [{'title': 'x', 'content': 'y', 'score': 0.20}]
        mock_graph.return_value = {'source': 'none', 'context': '', 'confidence': 0.10}
        mock_hybrid.return_value = {
            'chunks': [
                {'doc_title': '文档A', 'section_path': '章节1', 'content': '相关内容',
                 'chunk_id': 1, 'document_id': 1}
            ],
            'stats': {'vector_ms': 10, 'bm25_ms': 5},
        }
        user = MagicMock()
        from apps.graph.router import decide_route
        result = decide_route('测试问题', user)

        assert result['source'] == 'rag'
        assert len(result['chunks']) == 1
        assert '文档A' in result['context']
        assert result['route_trace'][2]['layer'] == 'rag'

    @patch('apps.llm.embedding.get_embedding_client')
    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_rag_empty_chunks(self, mock_hybrid, mock_graph, mock_wiki, mock_embed):
        """RAG 返回空 chunks，仍应返回 source='rag'，context 为空"""
        _patch_embed(mock_embed)
        mock_wiki.return_value = []
        mock_graph.return_value = {'source': 'none', 'context': '', 'confidence': 0.10}
        mock_hybrid.return_value = {'chunks': [], 'stats': {}}
        user = MagicMock()
        from apps.graph.router import decide_route
        result = decide_route('无相关问题', user)

        assert result['source'] == 'rag'
        assert result['context'] == ''

    @patch('apps.llm.embedding.get_embedding_client')
    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_rag_exception_handled(self, mock_hybrid, mock_graph, mock_wiki, mock_embed):
        """hybrid_search 抛异常时，应捕获并返回空 chunks"""
        _patch_embed(mock_embed)
        mock_wiki.return_value = []
        mock_graph.return_value = {'source': 'none', 'context': '', 'confidence': 0.10}
        mock_hybrid.side_effect = Exception('Embedding service unavailable')
        user = MagicMock()
        from apps.graph.router import decide_route
        result = decide_route('异常问题', user)

        assert result['source'] == 'rag'
        assert result['chunks'] == []

    @patch('apps.llm.embedding.get_embedding_client')
    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_route_trace_structure(self, mock_hybrid, mock_graph, mock_wiki, mock_embed):
        """验证 route_trace 每层都记录了 confidence 和 latency_ms"""
        _patch_embed(mock_embed)
        mock_wiki.return_value = [
            {'title': '高置信页', 'content': 'c', 'score': 0.75}
        ]
        mock_graph.return_value = {'source': 'graphrag', 'context': '', 'confidence': 0.10,
                                   'entities': [], 'relations': [], 'communities': []}
        mock_hybrid.return_value = {'chunks': [], 'stats': {}}
        user = MagicMock()
        from apps.graph.router import decide_route
        result = decide_route('测试', user)

        for entry in result['route_trace']:
            assert 'layer' in entry
            assert 'confidence' in entry
            assert 'latency_ms' in entry

    @patch('apps.llm.embedding.get_embedding_client')
    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_latency_ms_positive(self, mock_hybrid, mock_graph, mock_wiki, mock_embed):
        """路由总耗时应 >= 0"""
        _patch_embed(mock_embed)
        mock_wiki.return_value = [{'title': 'T', 'content': 'C', 'score': 0.50}]
        mock_graph.return_value = {'source': 'none', 'context': '', 'confidence': 0.30}
        mock_hybrid.return_value = {'chunks': [], 'stats': {}}
        user = MagicMock()
        from apps.graph.router import decide_route
        result = decide_route('测试', user)

        assert result['latency_ms'] >= 0


class TestFormatRagContext:
    """_format_rag_context 格式化测试"""

    def test_empty_chunks(self):
        from apps.graph.router import _format_rag_context
        assert _format_rag_context([]) == ''

    def test_single_chunk(self):
        from apps.graph.router import _format_rag_context
        chunks = [{'doc_title': 'Doc1', 'section_path': 'S1', 'content': 'Hello World'}]
        result = _format_rag_context(chunks)
        assert 'Doc1' in result
        assert 'Hello World' in result

    def test_multiple_chunks_max5(self):
        from apps.graph.router import _format_rag_context
        chunks = [
            {'doc_title': f'Doc{i}', 'section_path': f'S{i}', 'content': f'Content {i}' * 100}
            for i in range(10)
        ]
        result = _format_rag_context(chunks)
        # 最多 5 个 chunk
        assert result.count('[') == 5

    def test_content_truncated_to_500(self):
        from apps.graph.router import _format_rag_context
        long_content = 'A' * 1000
        chunks = [{'doc_title': 'Doc', 'section_path': 'S', 'content': long_content}]
        result = _format_rag_context(chunks)
        assert len(result) < 600


class TestOrchestrate:
    """orchestrate 入口测试"""

    @patch('apps.system.config_loader.get_config_value')
    @patch('apps.graph.router.decide_route')
    def test_orchestrate_calls_decide_route(self, mock_decide, mock_config):
        # orchestrate 默认读取 FAST_MODE_STRATEGY='rag_only'，需 patch 为 'parallel'
        # 才会走 decide_route（三路并行）分支
        mock_config.return_value = 'parallel'
        mock_decide.return_value = {'source': 'wiki', 'context': 'test'}
        from apps.graph.router import orchestrate
        user = MagicMock()
        result = orchestrate('q', user)
        mock_decide.assert_called_once_with('q', user, node_ids=None, root_types=None)
        assert result['source'] == 'wiki'


# ============================================================
# 以下为补充覆盖测试：子函数、策略分支、异常路径
# ============================================================


class TestSearchWiki:
    """_search_wiki 子任务：正常检索 + 异常降级"""

    @patch('apps.wiki.retriever.search_wiki')
    def test_success_returns_page_and_confidence(self, mock_wiki):
        """正常返回时应包含 confidence / page / trace"""
        mock_wiki.return_value = [
            {'title': '制度', 'content': '内容', 'score': 0.72}
        ]
        from apps.graph.router import _search_wiki
        result = _search_wiki('制度', [0.1, 0.2], threshold=0.55)

        mock_wiki.assert_called_once_with('制度', top_k=1, threshold=0.55,
                                          query_vector=[0.1, 0.2])
        assert result['confidence'] == 0.72
        assert result['page']['title'] == '制度'
        assert result['trace']['layer'] == 'wiki'
        assert result['trace']['confidence'] == 0.72
        assert result['trace']['latency_ms'] >= 0

    @patch('apps.wiki.retriever.search_wiki')
    def test_empty_results_returns_zero_confidence(self, mock_wiki):
        """search_wiki 返回空列表时，confidence 应为 0.0，page 为 None"""
        mock_wiki.return_value = []
        from apps.graph.router import _search_wiki
        result = _search_wiki('无关', None, threshold=0.55)

        assert result['confidence'] == 0.0
        assert result['page'] is None

    @patch('apps.wiki.retriever.search_wiki')
    def test_exception_returns_empty_results(self, mock_wiki):
        """search_wiki 抛异常时，应捕获并返回空 results（confidence=0）"""
        mock_wiki.side_effect = RuntimeError('wiki service down')
        from apps.graph.router import _search_wiki
        result = _search_wiki('查询', [0.1], threshold=0.55)

        assert result['confidence'] == 0.0
        assert result['page'] is None
        assert result['trace']['layer'] == 'wiki'


class TestSearchGraph:
    """_search_graph 子任务：正常检索 + 异常降级"""

    @patch('apps.graph.retriever.graphrag_search')
    def test_success_returns_graph_result(self, mock_graph):
        """正常返回时应包含 source / context / entities / communities"""
        mock_graph.return_value = {
            'source': 'graphrag_local', 'context': 'ctx',
            'confidence': 0.55,
            'entities': ['E1'], 'relations': [('a', 'b', 'c')],
            'communities': ['C1'],
        }
        user = MagicMock()
        from apps.graph.router import _search_graph
        result = _search_graph('问题', user, qvec=[0.1, 0.2])

        mock_graph.assert_called_once_with('问题', user, mode='auto',
                                           query_vector=[0.1, 0.2])
        assert result['confidence'] == 0.55
        assert result['source'] == 'graphrag_local'
        assert result['entities'] == ['E1']
        assert result['relations'] == [('a', 'b', 'c')]
        assert result['communities'] == ['C1']
        assert result['trace']['layer'] == 'graphrag'

    @patch('apps.graph.retriever.graphrag_search')
    def test_exception_returns_zero_confidence(self, mock_graph):
        """graphrag_search 抛异常时，应返回默认空结构（confidence=0）"""
        mock_graph.side_effect = ConnectionError('graph db unreachable')
        user = MagicMock()
        from apps.graph.router import _search_graph
        result = _search_graph('问题', user, qvec=None)

        assert result['confidence'] == 0.0
        assert result['source'] == 'graphrag'
        assert result['context'] == ''
        assert result['entities'] == []
        assert result['relations'] == []
        assert result['communities'] == []

    @patch('apps.graph.retriever.graphrag_search')
    def test_qvec_none_passed_through(self, mock_graph):
        """qvec 为 None 时应透传给 graphrag_search"""
        mock_graph.return_value = {'confidence': 0.1, 'source': 'g', 'context': '',
                                   'entities': [], 'relations': [], 'communities': []}
        user = MagicMock()
        from apps.graph.router import _search_graph
        _search_graph('q', user, qvec=None)

        mock_graph.assert_called_once_with('q', user, mode='auto', query_vector=None)


class TestSearchRag:
    """_search_rag 子任务：正常检索 + 异常降级"""

    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_success_returns_chunks(self, mock_hybrid):
        """正常返回时应包含 chunks / stats / confidence"""
        mock_hybrid.return_value = {
            'chunks': [{'doc_title': 'D1'}] * 3,
            'stats': {'vector_ms': 10},
        }
        user = MagicMock()
        from apps.graph.router import _search_rag
        result = _search_rag('q', user, node_ids=[1], root_types=['dept'],
                             qvec=[0.1])

        mock_hybrid.assert_called_once_with('q', user, do_rerank=True,
                                            node_ids=[1], root_types=['dept'],
                                            query_vector=[0.1])
        assert len(result['chunks']) == 3
        assert result['stats'] == {'vector_ms': 10}
        # confidence = min(1.0, 0.3 + 3 * 0.05) = 0.45
        assert result['confidence'] == 0.45
        assert result['trace']['layer'] == 'rag'

    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_exception_returns_empty_chunks(self, mock_hybrid):
        """hybrid_search 抛异常时应捕获，返回空 chunks"""
        mock_hybrid.side_effect = Exception('vector db timeout')
        user = MagicMock()
        from apps.graph.router import _search_rag
        result = _search_rag('q', user, node_ids=None, root_types=None, qvec=None)

        assert result['chunks'] == []
        assert result['stats'] == {}
        assert result['confidence'] == 0.3  # 0.3 + 0 * 0.05

    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_confidence_cap_at_one(self, mock_hybrid):
        """chunks 数量足够多时 confidence 不应超过 1.0"""
        mock_hybrid.return_value = {
            'chunks': [{'doc_title': 'D'}] * 20,
            'stats': {},
        }
        user = MagicMock()
        from apps.graph.router import _search_rag
        result = _search_rag('q', user, node_ids=None, root_types=None, qvec=None)

        assert result['confidence'] == 1.0


class TestCitationChunks:
    """_citation_chunks 引用补充检索"""

    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_returns_up_to_limit_chunks(self, mock_hybrid):
        """正常返回时应截取前 limit 个 chunks"""
        mock_hybrid.return_value = {
            'chunks': [{'doc_title': f'D{i}'} for i in range(10)],
            'stats': {},
        }
        user = MagicMock()
        from apps.graph.router import _citation_chunks
        result = _citation_chunks('q', user, node_ids=[1], root_types=['t'],
                                  query_vector=[0.1], limit=3)

        assert len(result) == 3
        assert result[0]['doc_title'] == 'D0'

    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_exception_returns_empty_list(self, mock_hybrid):
        """hybrid_search 抛异常时应返回空列表，不阻断主流程"""
        mock_hybrid.side_effect = RuntimeError('retrieval error')
        user = MagicMock()
        from apps.graph.router import _citation_chunks
        result = _citation_chunks('q', user, node_ids=None, root_types=None)

        assert result == []

    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_no_chunks_key_returns_empty(self, mock_hybrid):
        """返回字典中缺少 'chunks' 键时应返回空列表"""
        mock_hybrid.return_value = {'stats': {}}
        user = MagicMock()
        from apps.graph.router import _citation_chunks
        result = _citation_chunks('q', user, node_ids=None, root_types=None)

        assert result == []


class TestOrchestrateStrategies:
    """orchestrate 策略分发：sequential / rag_only"""

    @patch('apps.system.config_loader.get_config_value')
    @patch('apps.graph.router._decide_route_sequential')
    def test_sequential_strategy(self, mock_seq, mock_config):
        """strategy='sequential' 时应调用 _decide_route_sequential"""
        mock_config.return_value = 'sequential'
        mock_seq.return_value = {'source': 'wiki', 'context': ''}
        user = MagicMock()
        from apps.graph.router import orchestrate
        result = orchestrate('q', user, node_ids=[1], root_types=['t'])

        mock_seq.assert_called_once_with('q', user, node_ids=[1], root_types=['t'])
        assert result['source'] == 'wiki'

    @patch('apps.system.config_loader.get_config_value')
    @patch('apps.graph.router._decide_route_rag_only')
    def test_rag_only_strategy(self, mock_rag, mock_config):
        """strategy='rag_only'（默认）时应调用 _decide_route_rag_only"""
        mock_config.return_value = 'rag_only'
        mock_rag.return_value = {'source': 'rag', 'context': 'ctx'}
        user = MagicMock()
        from apps.graph.router import orchestrate
        result = orchestrate('q', user)

        mock_rag.assert_called_once_with('q', user, node_ids=None, root_types=None)
        assert result['source'] == 'rag'

    @patch('apps.system.config_loader.get_config_value')
    @patch('apps.graph.router._decide_route_rag_only')
    def test_unknown_strategy_falls_back_to_rag_only(self, mock_rag, mock_config):
        """未识别的 strategy 值应走 rag_only 分支（else 分支）"""
        mock_config.return_value = 'unknown_strategy'
        mock_rag.return_value = {'source': 'rag', 'context': ''}
        user = MagicMock()
        from apps.graph.router import orchestrate
        result = orchestrate('q', user)

        mock_rag.assert_called_once()
        assert result['source'] == 'rag'


class TestDecideRouteSequential:
    """_decide_route_sequential 串行降级路由"""

    @patch('apps.llm.embedding.get_embedding_client')
    def test_embedding_exception_sets_qvec_none(self, mock_embed):
        """EmbeddingException 应被捕获，qvec 设为 None，后续检索仍继续"""
        from apps.llm.embedding import EmbeddingException
        mock_client = MagicMock()
        mock_client.embed_one.side_effect = EmbeddingException('timeout')
        mock_embed.return_value = mock_client

        user = MagicMock()
        from apps.graph.router import _decide_route_sequential
        with patch('apps.wiki.retriever.search_wiki', return_value=[]) as m_wiki, \
             patch('apps.graph.retriever.graphrag_search',
                   return_value={'confidence': 0.0, 'source': 'g', 'context': '',
                                 'entities': [], 'relations': [], 'communities': []}), \
             patch('apps.retrieval.hybrid.hybrid_search',
                   return_value={'chunks': [], 'stats': {}}):
            result = _decide_route_sequential('q', user)
            # qvec=None 被透传给 search_wiki
            m_wiki.assert_called_once_with('q', top_k=1, threshold=0.55, query_vector=None)
            assert result['source'] == 'rag'

    @patch('apps.llm.embedding.get_embedding_client')
    def test_zero_vector_sets_qvec_none(self, mock_embed):
        """全零向量应被视为无效，qvec 设为 None"""
        mock_client = MagicMock()
        mock_client.embed_one.return_value = [0.0, 0.0, 0.0]
        mock_embed.return_value = mock_client

        user = MagicMock()
        from apps.graph.router import _decide_route_sequential
        with patch('apps.wiki.retriever.search_wiki', return_value=[]) as m_wiki, \
             patch('apps.graph.retriever.graphrag_search',
                   return_value={'confidence': 0.0, 'source': 'g', 'context': '',
                                 'entities': [], 'relations': [], 'communities': []}), \
             patch('apps.retrieval.hybrid.hybrid_search',
                   return_value={'chunks': [], 'stats': {}}):
            result = _decide_route_sequential('q', user)
            m_wiki.assert_called_once_with('q', top_k=1, threshold=0.55, query_vector=None)
            assert result['source'] == 'rag'

    @patch('apps.llm.embedding.get_embedding_client')
    @patch('apps.retrieval.hybrid.hybrid_search')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.wiki.retriever.search_wiki')
    def test_wiki_hit_stops_early(self, mock_wiki, mock_graph, mock_hybrid, mock_embed):
        """Wiki 命中后应直接返回，不继续调用 GraphRAG（citation_chunks 会调用 hybrid_search）"""
        _patch_embed(mock_embed)
        mock_wiki.return_value = [
            {'title': 'Wiki页', 'content': '内容', 'score': 0.80}
        ]
        mock_hybrid.return_value = {'chunks': [], 'stats': {}}
        user = MagicMock()
        from apps.graph.router import _decide_route_sequential
        result = _decide_route_sequential('q', user)

        assert result['source'] == 'wiki'
        assert result['confidence'] == 0.80
        assert 'Wiki页' in result['context']
        # Wiki 命中后不应调用 GraphRAG（_citation_chunks 会调用 hybrid_search 补充引用）
        mock_graph.assert_not_called()
        # route_trace 只有 wiki 一层
        assert len(result['route_trace']) == 1
        assert result['route_trace'][0]['layer'] == 'wiki'

    @patch('apps.llm.embedding.get_embedding_client')
    @patch('apps.retrieval.hybrid.hybrid_search')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.wiki.retriever.search_wiki')
    def test_graphrag_hit_stops_early(self, mock_wiki, mock_graph, mock_hybrid, mock_embed):
        """Wiki 未命中、GraphRAG 命中后应直接返回（citation_chunks 会调用 hybrid_search）"""
        _patch_embed(mock_embed)
        mock_wiki.return_value = [{'title': 'x', 'content': 'y', 'score': 0.30}]
        mock_graph.return_value = {
            'source': 'graphrag_global', 'context': 'graph ctx',
            'confidence': 0.55,
            'entities': ['E1'], 'relations': [], 'communities': ['C1'],
        }
        mock_hybrid.return_value = {'chunks': [], 'stats': {}}
        user = MagicMock()
        from apps.graph.router import _decide_route_sequential
        result = _decide_route_sequential('q', user)

        assert result['source'] == 'graphrag_global'
        assert result['confidence'] == 0.55
        assert len(result['route_trace']) == 2
        assert result['route_trace'][1]['layer'] == 'graphrag'

    @patch('apps.llm.embedding.get_embedding_client')
    @patch('apps.retrieval.hybrid.hybrid_search')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.wiki.retriever.search_wiki')
    def test_all_miss_fallback_rag(self, mock_wiki, mock_graph, mock_hybrid, mock_embed):
        """三层都未命中时应回退到 RAG 兜底"""
        _patch_embed(mock_embed)
        mock_wiki.return_value = [{'title': 'x', 'content': 'y', 'score': 0.20}]
        mock_graph.return_value = {
            'source': 'none', 'context': '', 'confidence': 0.10,
            'entities': [], 'relations': [], 'communities': [],
        }
        mock_hybrid.return_value = {
            'chunks': [{'doc_title': 'DocA', 'section_path': 'S',
                        'content': 'c', 'chunk_id': 1, 'document_id': 1}],
            'stats': {},
        }
        user = MagicMock()
        from apps.graph.router import _decide_route_sequential
        result = _decide_route_sequential('q', user)

        assert result['source'] == 'rag'
        assert len(result['chunks']) == 1
        assert len(result['route_trace']) == 3

    @patch('apps.llm.embedding.get_embedding_client')
    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_wiki_exception_continues_to_graphrag(self, mock_hybrid, mock_graph,
                                                  mock_wiki, mock_embed):
        """Wiki 检索异常时应降级到 GraphRAG / RAG"""
        _patch_embed(mock_embed)
        mock_wiki.side_effect = RuntimeError('wiki down')
        mock_graph.return_value = {
            'source': 'graphrag_local', 'context': 'gc',
            'confidence': 0.50,
            'entities': [], 'relations': [], 'communities': [],
        }
        mock_hybrid.return_value = {'chunks': [], 'stats': {}}
        user = MagicMock()
        from apps.graph.router import _decide_route_sequential
        result = _decide_route_sequential('q', user)

        # GraphRAG confidence 0.50 >= 0.45 阈值
        assert result['source'] == 'graphrag_local'
        assert result['confidence'] == 0.50

    @patch('apps.llm.embedding.get_embedding_client')
    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_graphrag_exception_continues_to_rag(self, mock_hybrid, mock_graph,
                                                 mock_wiki, mock_embed):
        """GraphRAG 检索异常时应降级到 RAG 兜底"""
        _patch_embed(mock_embed)
        mock_wiki.return_value = [{'title': 'x', 'content': 'y', 'score': 0.20}]
        mock_graph.side_effect = RuntimeError('graph db down')
        mock_hybrid.return_value = {
            'chunks': [{'doc_title': 'D', 'section_path': '', 'content': 'c'}],
            'stats': {},
        }
        user = MagicMock()
        from apps.graph.router import _decide_route_sequential
        result = _decide_route_sequential('q', user)

        assert result['source'] == 'rag'
        assert len(result['chunks']) == 1

    @patch('apps.llm.embedding.get_embedding_client')
    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_rag_exception_returns_empty(self, mock_hybrid, mock_graph,
                                         mock_wiki, mock_embed):
        """RAG 兜底检索异常时应返回空 chunks"""
        _patch_embed(mock_embed)
        mock_wiki.return_value = [{'title': 'x', 'content': 'y', 'score': 0.20}]
        mock_graph.return_value = {
            'source': 'none', 'context': '', 'confidence': 0.10,
            'entities': [], 'relations': [], 'communities': [],
        }
        mock_hybrid.side_effect = Exception('rag error')
        user = MagicMock()
        from apps.graph.router import _decide_route_sequential
        result = _decide_route_sequential('q', user)

        assert result['source'] == 'rag'
        assert result['chunks'] == []
        assert len(result['route_trace']) == 3


class TestDecideRouteRagOnly:
    """_decide_route_rag_only 纯 RAG 模式"""

    @patch('apps.retrieval.hybrid._search_core')
    def test_success_returns_chunks(self, mock_core):
        """正常返回时应包含 chunks / confidence / trace"""
        mock_core.return_value = {
            'chunks': [{'doc_title': 'D1'}, {'doc_title': 'D2'}],
            'stats': {'bm25_ms': 5},
        }
        user = MagicMock()
        from apps.graph.router import _decide_route_rag_only
        result = _decide_route_rag_only('q', user, node_ids=[1], root_types=['dept'])

        mock_core.assert_called_once_with('q', user, do_rerank=True,
                                          node_ids=[1], root_types=['dept'])
        assert len(result['chunks']) == 2
        # confidence = min(1.0, 0.3 + 2 * 0.05) = 0.40
        assert result['confidence'] == 0.40
        assert result['source'] == 'rag'
        assert result['route_trace'][-1]['layer'] == 'rag'
        assert result['route_trace'][-1]['latency_ms'] >= 0

    @patch('apps.retrieval.hybrid._search_core')
    def test_exception_returns_empty(self, mock_core):
        """_search_core 抛异常时应捕获，返回空结果（chunks 为空，confidence=0.3）"""
        mock_core.side_effect = ConnectionError('db down')
        user = MagicMock()
        from apps.graph.router import _decide_route_rag_only
        result = _decide_route_rag_only('q', user)

        # 异常被捕获后 rag_result 为 {'chunks': [], 'stats': {}}，不会走到 result.get
        assert result['chunks'] == []
        assert result['confidence'] == 0.3  # 0.3 + 0 chunks
        assert result['source'] == 'rag'

    @patch('apps.retrieval.hybrid._search_core')
    def test_exception_fallback_empty_structure(self, mock_core):
        """_search_core 返回缺少 chunks 键的字典时，chunks 应为空列表"""
        mock_core.return_value = {'stats': {}}
        user = MagicMock()
        from apps.graph.router import _decide_route_rag_only
        result = _decide_route_rag_only('q', user)

        assert result['chunks'] == []
        assert result['confidence'] == 0.3
        assert result['source'] == 'rag'
        assert result['route_trace'][-1]['latency_ms'] >= 0

    @patch('apps.retrieval.hybrid._search_core')
    def test_empty_chunks(self, mock_core):
        """空 chunks 时 confidence 应为 0.3"""
        mock_core.return_value = {'chunks': [], 'stats': {}}
        user = MagicMock()
        from apps.graph.router import _decide_route_rag_only
        result = _decide_route_rag_only('q', user)

        assert result['confidence'] == 0.3
        assert result['context'] == ''

    @patch('apps.retrieval.hybrid._search_core')
    def test_no_node_ids_or_root_types(self, mock_core):
        """node_ids / root_types 为 None 时应透传"""
        mock_core.return_value = {'chunks': [], 'stats': {}}
        user = MagicMock()
        from apps.graph.router import _decide_route_rag_only
        _decide_route_rag_only('q', user, node_ids=None, root_types=None)

        mock_core.assert_called_once_with('q', user, do_rerank=True,
                                          node_ids=None, root_types=None)


class TestDecideRouteEmbeddingFailure:
    """decide_route 中 embedding 失败的降级处理"""

    @patch('apps.llm.embedding.get_embedding_client')
    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_embedding_exception_qvec_none(self, mock_hybrid, mock_graph,
                                           mock_wiki, mock_embed):
        """EmbeddingException 应被捕获，qvec=None 透传给三路检索"""
        from apps.llm.embedding import EmbeddingException
        mock_client = MagicMock()
        mock_client.embed_one.side_effect = EmbeddingException('service error')
        mock_embed.return_value = mock_client

        mock_wiki.return_value = []
        mock_graph.return_value = {
            'source': 'none', 'context': '', 'confidence': 0.10,
            'entities': [], 'relations': [], 'communities': [],
        }
        mock_hybrid.return_value = {
            'chunks': [{'doc_title': 'D1'}],
            'stats': {},
        }
        user = MagicMock()
        from apps.graph.router import decide_route
        result = decide_route('q', user)

        # qvec=None 传给 search_wiki
        mock_wiki.assert_called_once_with('q', top_k=1, threshold=0.55,
                                          query_vector=None)
        assert result['source'] == 'rag'

    @patch('apps.llm.embedding.get_embedding_client')
    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_zero_vector_qvec_none(self, mock_hybrid, mock_graph,
                                   mock_wiki, mock_embed):
        """全零向量应被视为无效，qvec 设为 None"""
        mock_client = MagicMock()
        mock_client.embed_one.return_value = [0.0, 0.0, 0.0]
        mock_embed.return_value = mock_client

        mock_wiki.return_value = []
        mock_graph.return_value = {
            'source': 'none', 'context': '', 'confidence': 0.10,
            'entities': [], 'relations': [], 'communities': [],
        }
        mock_hybrid.return_value = {'chunks': [], 'stats': {}}
        user = MagicMock()
        from apps.graph.router import decide_route
        result = decide_route('q', user)

        mock_wiki.assert_called_once_with('q', top_k=1, threshold=0.55,
                                          query_vector=None)
        assert result['source'] == 'rag'

    @patch('apps.llm.embedding.get_embedding_client')
    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_wiki_exception_still_runs_graph_and_rag(self, mock_hybrid, mock_graph,
                                                     mock_wiki, mock_embed):
        """并行模式下 Wiki 异常不应影响 GraphRAG 和 RAG 的执行"""
        _patch_embed(mock_embed)
        mock_wiki.side_effect = RuntimeError('wiki crash')
        mock_graph.return_value = {
            'source': 'none', 'context': '', 'confidence': 0.10,
            'entities': [], 'relations': [], 'communities': [],
        }
        mock_hybrid.return_value = {
            'chunks': [{'doc_title': 'fallback'}],
            'stats': {},
        }
        user = MagicMock()
        from apps.graph.router import decide_route
        result = decide_route('q', user)

        # 三路并行，Wiki 异常但 GraphRAG / RAG 仍被调用
        mock_graph.assert_called_once()
        assert mock_hybrid.call_count >= 1  # RAG 兜底 + 可能的 citation
        assert result['source'] == 'rag'

    @patch('apps.llm.embedding.get_embedding_client')
    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_graphrag_exception_still_returns_result(self, mock_hybrid, mock_graph,
                                                     mock_wiki, mock_embed):
        """并行模式下 GraphRAG 异常不应影响其他两路"""
        _patch_embed(mock_embed)
        mock_wiki.return_value = [{'title': 'W', 'content': 'C', 'score': 0.30}]
        mock_graph.side_effect = ConnectionError('graph error')
        mock_hybrid.return_value = {'chunks': [], 'stats': {}}
        user = MagicMock()
        from apps.graph.router import decide_route
        result = decide_route('q', user)

        # GraphRAG 异常后 confidence=0，不命中阈值，最终走 RAG
        assert result['source'] == 'rag'


class TestFormatRagContextEdgeCases:
    """_format_rag_context 边界条件补充"""

    def test_chunk_missing_fields(self):
        """chunk 缺少 doc_title / section_path / content 字段时应使用默认值"""
        from apps.graph.router import _format_rag_context
        chunks = [{}]
        result = _format_rag_context(chunks)
        assert '未知文档' in result

    def test_section_path_empty_skips_parens(self):
        """section_path 为空时不显示括号"""
        from apps.graph.router import _format_rag_context
        chunks = [{'doc_title': 'D', 'section_path': '', 'content': 'text'}]
        result = _format_rag_context(chunks)
        assert '（' not in result

    def test_section_path_present_shows_parens(self):
        """section_path 非空时应显示括号包裹的路径"""
        from apps.graph.router import _format_rag_context
        chunks = [{'doc_title': 'D', 'section_path': 'Chapter1', 'content': 'text'}]
        result = _format_rag_context(chunks)
        assert '（Chapter1）' in result

    def test_none_chunks_returns_empty(self):
        """传入 None 时应返回空字符串"""
        from apps.graph.router import _format_rag_context
        assert _format_rag_context(None) == ''

    def test_exactly_five_chunks(self):
        """恰好 5 个 chunk 时应全部输出"""
        from apps.graph.router import _format_rag_context
        chunks = [
            {'doc_title': f'D{i}', 'section_path': '', 'content': f'c{i}'}
            for i in range(5)
        ]
        result = _format_rag_context(chunks)
        for i in range(5):
            assert f'D{i}' in result
