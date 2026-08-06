"""
agent.tools.wiki_search / graph_search 单元测试

覆盖：
- WikiSearchTool：命中/未命中分支、内容格式化（title+summary+content 截断）、meta 字段
- GraphSearchTool：命中/未命中分支、mode 参数透传、meta 字段（entities/relations/communities）

纯逻辑测试，mock wiki/graph retriever，不依赖 DB。
"""
import pytest
from unittest.mock import patch, MagicMock

from apps.agent.tools.base import ToolContext
from apps.agent.tools.wiki_search import WikiSearchTool, WIKI_HIT_THRESHOLD
from apps.agent.tools.graph_search import GraphSearchTool

pytestmark = pytest.mark.unit


# ============================================================================
# WikiSearchTool
# ============================================================================
class TestWikiSearchExecute:
    """WikiSearchTool.execute() 全部分支"""

    @patch('apps.wiki.retriever.search_wiki')
    def test_execute_when_no_results_then_returns_empty_message(self, mock_search):
        """未命中 Wiki 页面：返回未找到提示 + meta.hit=False"""
        mock_search.return_value = []
        tool = WikiSearchTool()
        ret = tool.execute(ToolContext(), query='不存在的概念')

        mock_search.assert_called_once_with(
            '不存在的概念', top_k=1, threshold=WIKI_HIT_THRESHOLD)
        assert ret['ok'] is True
        assert '未找到' in ret['result']
        assert ret['meta']['hit'] is False
        assert ret['meta']['wiki_pages'] == []

    @patch('apps.wiki.retriever.search_wiki')
    def test_execute_when_hit_then_returns_formatted_content(self, mock_search):
        """命中 Wiki 页面：返回格式化内容（标题 + 摘要 + 正文）"""
        mock_search.return_value = [{
            'wiki_id': 42,
            'title': 'RAG 是什么',
            'summary': '检索增强生成的简述',
            'content': 'RAG 的完整正文内容',
        }]
        tool = WikiSearchTool()
        ret = tool.execute(ToolContext(), query='RAG')

        assert ret['ok'] is True
        assert ret['meta']['hit'] is True
        assert ret['meta']['wiki_id'] == 42
        assert ret['meta']['wiki_pages'][0]['title'] == 'RAG 是什么'
        # 结果文本包含标题、摘要、正文三段
        assert '# RAG 是什么' in ret['result']
        assert '检索增强生成的简述' in ret['result']
        assert 'RAG 的完整正文内容' in ret['result']

    @patch('apps.wiki.retriever.search_wiki')
    def test_execute_when_long_content_then_truncated_to_2000(self, mock_search):
        """Wiki 正文超过 2000 字时截断，避免 context 过长"""
        long_content = 'x' * 3000
        mock_search.return_value = [{
            'wiki_id': 1, 'title': '长文', 'summary': '摘要',
            'content': long_content,
        }]
        tool = WikiSearchTool()
        ret = tool.execute(ToolContext(), query='长文')

        # 结果应包含截断后的内容（前 2000 字），但不包含全部 3000 字
        assert 'x' * 2000 in ret['result']
        assert 'x' * 3000 not in ret['result']

    @patch('apps.wiki.retriever.search_wiki')
    def test_execute_when_multiple_results_then_only_first_used(self, mock_search):
        """top_k=1：即便 retriever 返回多条，也只使用第一条"""
        mock_search.return_value = [
            {'wiki_id': 1, 'title': '第一条', 'summary': 's1', 'content': 'c1'},
            {'wiki_id': 2, 'title': '第二条', 'summary': 's2', 'content': 'c2'},
        ]
        tool = WikiSearchTool()
        ret = tool.execute(ToolContext(), query='测试')

        assert ret['meta']['wiki_id'] == 1
        assert '第一条' in ret['result']
        assert '第二条' not in ret['result']


# ============================================================================
# GraphSearchTool
# ============================================================================
class TestGraphSearchExecute:
    """GraphSearchTool.execute() 全部分支"""

    @patch('apps.graph.retriever.graphrag_search')
    def test_execute_when_no_context_then_returns_empty_message(self, mock_search):
        """未命中图谱：返回未找到提示 + meta.hit=False"""
        mock_search.return_value = {'context': '', 'entities': [], 'relations': []}
        tool = GraphSearchTool()
        ret = tool.execute(ToolContext(), query='不存在的关系')

        assert ret['ok'] is True
        assert '未在知识图谱中找到' in ret['result']
        assert ret['meta']['hit'] is False
        assert ret['meta']['entities'] == []
        assert ret['meta']['relations'] == []

    @patch('apps.graph.retriever.graphrag_search')
    def test_execute_when_no_context_key_then_returns_empty_message(self, mock_search):
        """返回结果不含 context 键：视为未命中"""
        mock_search.return_value = {'entities': [], 'relations': []}
        tool = GraphSearchTool()
        ret = tool.execute(ToolContext(), query='测试')

        assert ret['ok'] is True
        assert '未在知识图谱中找到' in ret['result']
        assert ret['meta']['hit'] is False

    @patch('apps.graph.retriever.graphrag_search')
    def test_execute_when_hit_then_returns_context_and_meta(self, mock_search):
        """命中图谱：返回 context 文本 + meta 含实体/关系/社区"""
        mock_search.return_value = {
            'context': '张三 -> 负责 -> 项目A',
            'entities': [{'name': '张三'}, {'name': '项目A'}],
            'relations': [{'type': '负责'}],
            'communities': [{'id': 1, 'summary': '社区摘要'}],
            'source': 'local',
        }
        tool = GraphSearchTool()
        ret = tool.execute(ToolContext(), query='张三负责什么')

        assert ret['ok'] is True
        assert ret['meta']['hit'] is True
        assert ret['result'] == '张三 -> 负责 -> 项目A'
        assert len(ret['meta']['entities']) == 2
        assert len(ret['meta']['relations']) == 1
        assert ret['meta']['communities'][0]['id'] == 1
        assert ret['meta']['graph_source'] == 'local'

    @patch('apps.graph.retriever.graphrag_search')
    def test_execute_when_mode_specified_then_passed_through(self, mock_search):
        """mode 参数应透传给 graphrag_search"""
        mock_search.return_value = {'context': '局部结果'}
        tool = GraphSearchTool()
        tool.execute(ToolContext(), query='测试', mode='local')
        mock_search.assert_called_once()
        args, kwargs = mock_search.call_args
        # graphrag_search(query, user, mode=mode)
        assert kwargs.get('mode') == 'local'

    @patch('apps.graph.retriever.graphrag_search')
    def test_execute_when_no_mode_then_defaults_to_auto(self, mock_search):
        """未传 mode 时默认 'auto'"""
        mock_search.return_value = {'context': '自动模式结果'}
        tool = GraphSearchTool()
        tool.execute(ToolContext(), query='测试')
        mock_search.assert_called_once()
        args, kwargs = mock_search.call_args
        assert kwargs.get('mode') == 'auto'

    @patch('apps.graph.retriever.graphrag_search')
    def test_execute_when_user_provided_then_passed_to_search(self, mock_search):
        """ctx.user 应透传给 graphrag_search（用于权限过滤预留）"""
        mock_search.return_value = {'context': '结果'}
        tool = GraphSearchTool()
        user = MagicMock(id=99)
        tool.execute(ToolContext(user=user), query='测试')
        mock_search.assert_called_once()
        args, kwargs = mock_search.call_args
        # 第二个位置参数为 user
        assert args[1] is user or kwargs.get('user') is user

    @patch('apps.graph.retriever.graphrag_search')
    def test_execute_when_missing_optional_fields_then_defaults_empty(self, mock_search):
        """返回结果缺失 entities/relations/communities 时默认空列表"""
        mock_search.return_value = {'context': '只有上下文', 'source': 'global'}
        tool = GraphSearchTool()
        ret = tool.execute(ToolContext(), query='测试')

        assert ret['ok'] is True
        assert ret['meta']['entities'] == []
        assert ret['meta']['relations'] == []
        assert ret['meta']['communities'] == []
        assert ret['meta']['graph_source'] == 'global'

    @patch('apps.graph.retriever.graphrag_search')
    def test_execute_when_empty_context_then_treated_as_miss(self, mock_search):
        """context 为空字符串时视为未命中"""
        mock_search.return_value = {'context': '', 'entities': [1], 'relations': [2]}
        tool = GraphSearchTool()
        ret = tool.execute(ToolContext(), query='测试')

        assert ret['meta']['hit'] is False
        assert '未在知识图谱中找到' in ret['result']
