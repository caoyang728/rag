"""
graph.router 单元测试
覆盖：decide_route 三层路由决策逻辑（Wiki → GraphRAG → RAG 兜底）
使用 mock 在源模块级别替换 search_wiki / graphrag_search / hybrid_search
"""
import pytest
from unittest.mock import patch, MagicMock


class TestDecideRouteWiki:
    """第 1 层：Wiki 快速命中"""

    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_wiki_direct_hit(self, mock_hybrid, mock_graph, mock_wiki):
        """Wiki 直接命中：score >= 0.68 时应返回 source='wiki'"""
        mock_wiki.return_value = [
            {'title': '公司请假制度', 'content': '员工可请假', 'score': 0.85}
        ]
        # Wiki 页面本身不携带 chunks：命中后补充 RAG 检索结果仅用于来源卡片
        mock_hybrid.return_value = {
            'chunks': [{'doc_title': '请假制度文档', 'section_path': 'S1',
                        'content': 'xx', 'chunk_id': 1, 'document_id': 1}],
            'stats': {},
        }
        user = MagicMock()
        from apps.graph.router import decide_route
        result = decide_route('请假流程', user)

        assert result['source'] == 'wiki'
        assert result['confidence'] == 0.85
        assert '公司请假制度' in result['context']
        # Wiki 直接命中时提前返回，route_trace 只有 1 层
        assert len(result['route_trace']) == 1
        assert result['route_trace'][0]['layer'] == 'wiki'
        # GraphRAG 不应被调用；hybrid 被调用仅用于补充引用 chunks
        mock_graph.assert_not_called()
        mock_hybrid.assert_called_once()
        assert len(result['chunks']) == 1

    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_wiki_not_hit_graphrag_succeeds(self, mock_hybrid, mock_graph, mock_wiki):
        """Wiki 未命中(score<0.68)，GraphRAG 命中"""
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
        # 图谱上下文不携带 chunks：命中后补充 RAG 检索结果仅用于来源卡片
        mock_hybrid.return_value = {
            'chunks': [{'doc_title': '张三档案', 'section_path': 'S1',
                        'content': 'xx', 'chunk_id': 2, 'document_id': 2}],
            'stats': {},
        }
        user = MagicMock()
        from apps.graph.router import decide_route
        result = decide_route('张三负责什么', user)

        assert result['source'] == 'graphrag_local'
        assert result['confidence'] == 0.60
        assert result['route_trace'][1]['layer'] == 'graphrag'
        # hybrid 被调用仅用于补充引用 chunks
        mock_hybrid.assert_called_once()
        assert len(result['chunks']) == 1

    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_fallback_to_rag(self, mock_hybrid, mock_graph, mock_wiki):
        """Wiki 和 GraphRAG 均未命中，回退到 RAG"""
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

    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_rag_empty_chunks(self, mock_hybrid, mock_graph, mock_wiki):
        """RAG 返回空 chunks，仍应返回 source='rag'，context 为空"""
        mock_wiki.return_value = []
        mock_graph.return_value = {'source': 'none', 'context': '', 'confidence': 0.10}
        mock_hybrid.return_value = {'chunks': [], 'stats': {}}
        user = MagicMock()
        from apps.graph.router import decide_route
        result = decide_route('无相关问题', user)

        assert result['source'] == 'rag'
        assert result['context'] == ''

    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_rag_exception_handled(self, mock_hybrid, mock_graph, mock_wiki):
        """hybrid_search 抛异常时，应捕获并返回空 chunks"""
        mock_wiki.return_value = []
        mock_graph.return_value = {'source': 'none', 'context': '', 'confidence': 0.10}
        mock_hybrid.side_effect = Exception('Embedding service unavailable')
        user = MagicMock()
        from apps.graph.router import decide_route
        result = decide_route('异常问题', user)

        assert result['source'] == 'rag'
        assert result['chunks'] == []

    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_route_trace_structure(self, mock_hybrid, mock_graph, mock_wiki):
        """验证 route_trace 每层都记录了 confidence 和 latency_ms"""
        mock_wiki.return_value = [
            {'title': '高置信页', 'content': 'c', 'score': 0.75}
        ]
        user = MagicMock()
        from apps.graph.router import decide_route
        result = decide_route('测试', user)

        for entry in result['route_trace']:
            assert 'layer' in entry
            assert 'confidence' in entry
            assert 'latency_ms' in entry

    @patch('apps.wiki.retriever.search_wiki')
    @patch('apps.graph.retriever.graphrag_search')
    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_latency_ms_positive(self, mock_hybrid, mock_graph, mock_wiki):
        """路由总耗时应 >= 0"""
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

    @patch('apps.graph.router.decide_route')
    def test_orchestrate_calls_decide_route(self, mock_decide):
        mock_decide.return_value = {'source': 'wiki', 'context': 'test'}
        from apps.graph.router import orchestrate
        user = MagicMock()
        result = orchestrate('q', user)
        mock_decide.assert_called_once_with('q', user, node_ids=None, root_types=None)
        assert result['source'] == 'wiki'
