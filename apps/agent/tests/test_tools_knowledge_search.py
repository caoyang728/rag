"""
agent.tools.knowledge_search 单元测试

覆盖 KnowledgeSearchTool 全部分支：
- execute() 主流程：正常检索、无结果、Embedding 异常、通用异常
- 二次权限过滤：已登录用户过滤无权文档、匿名用户跳过、全部被过滤
- top_k 参数防御：截断到 1-10 范围
- 结果格式化：内容截断、位置信息（章节/页码）

"""
import pytest
from unittest.mock import patch, MagicMock

from apps.agent.tools.base import ToolContext
from apps.agent.tools.knowledge_search import KnowledgeSearchTool

pytestmark = pytest.mark.unit


def _chunk(cid, doc_id, title='文档A', section='s1', page=1, content='片段内容'):
    """构造测试用 chunk"""
    return {
        'chunk_id': cid, 'document_id': doc_id, 'doc_title': title,
        'section_path': section, 'page_number': page, 'content': content,
    }


class TestKnowledgeSearchExecute:
    """execute() 主流程"""

    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_normal_search_returns_formatted_result(self, mock_hybrid):
        """正常检索：返回格式化文本 + meta 含 chunks/chunk_ids/doc_ids"""
        mock_hybrid.return_value = {
            'chunks': [_chunk(1, 101, title='文档A', content='正文内容')],
            'stats': {},
        }
        tool = KnowledgeSearchTool()
        ctx = ToolContext(user=None)
        ret = tool.execute(ctx, query='测试问题')

        assert ret['ok'] is True
        assert '文档A' in ret['result']
        assert '正文内容' in ret['result']
        assert ret['meta']['chunk_ids'] == [1]
        assert ret['meta']['doc_ids'] == [101]

    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_no_chunks_returns_empty_message(self, mock_hybrid):
        """检索无结果：返回未找到提示"""
        mock_hybrid.return_value = {'chunks': [], 'stats': {}}
        tool = KnowledgeSearchTool()
        ret = tool.execute(ToolContext(), query='不存在的内容')
        assert ret['ok'] is True
        assert '未在知识库中检索到' in ret['result']
        assert ret['meta']['chunks'] == []

    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_embedding_exception_returns_error(self, mock_hybrid):
        """Embedding 异常：返回工具不可用提示 + ok=False"""
        from apps.llm.embedding import EmbeddingException
        mock_hybrid.side_effect = EmbeddingException('embedding down')
        tool = KnowledgeSearchTool()
        ret = tool.execute(ToolContext(), query='问题')
        assert ret['ok'] is False
        assert '向量检索服务暂时不可用' in ret['result']
        assert ret['meta']['error'] == 'embedding_error'

    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_generic_exception_returns_error(self, mock_hybrid):
        """通用异常：返回检索失败信息 + 截断错误内容"""
        mock_hybrid.side_effect = RuntimeError('db connection lost')
        tool = KnowledgeSearchTool()
        ret = tool.execute(ToolContext(), query='问题')
        assert ret['ok'] is False
        assert '知识库检索失败' in ret['result']
        assert 'RuntimeError' in ret['result']
        assert ret['meta']['error'] == 'db connection lost'


class TestKnowledgeSearchPermissionFilter:
    """二次权限过滤逻辑"""

    @patch('apps.retrieval.hybrid.hybrid_search')
    @patch('apps.knowledge.access.filter_accessible_doc_ids')
    def test_execute_when_authenticated_then_filters_inaccessible(self, mock_filter, mock_hybrid):
        """已登录用户：过滤无权访问的文档片段"""
        mock_hybrid.return_value = {
            'chunks': [_chunk(1, 101, title='A'), _chunk(2, 102, title='B')],
            'stats': {},
        }
        mock_filter.return_value = [101]  # 只有 doc 101 可访问
        tool = KnowledgeSearchTool()
        user = MagicMock(is_authenticated=True, id=1)
        ret = tool.execute(ToolContext(user=user), query='问题')
        mock_filter.assert_called_once_with(user, [101, 102])
        assert len(ret['meta']['chunks']) == 1
        assert ret['meta']['chunks'][0]['document_id'] == 101

    @patch('apps.retrieval.hybrid.hybrid_search')
    @patch('apps.knowledge.access.filter_accessible_doc_ids')
    def test_execute_when_all_chunks_filtered_then_returns_permission_denied(self, mock_filter, mock_hybrid):
        """全部片段被权限过滤后：返回无权访问提示"""
        mock_hybrid.return_value = {
            'chunks': [_chunk(1, 101)],
            'stats': {},
        }
        mock_filter.return_value = []  # 全部无权访问
        tool = KnowledgeSearchTool()
        user = MagicMock(is_authenticated=True, id=1)
        ret = tool.execute(ToolContext(user=user), query='问题')
        assert ret['ok'] is True
        assert '无权访问' in ret['result']
        assert ret['meta']['permission_denied'] is True

    @patch('apps.retrieval.hybrid.hybrid_search')
    @patch('apps.knowledge.access.filter_accessible_doc_ids')
    def test_execute_when_anonymous_then_skips_permission_filter(self, mock_filter, mock_hybrid):
        """匿名用户：跳过二次权限验证"""
        mock_hybrid.return_value = {
            'chunks': [_chunk(1, 101)],
            'stats': {},
        }
        tool = KnowledgeSearchTool()
        ret = tool.execute(ToolContext(user=None), query='问题')
        mock_filter.assert_not_called()
        assert ret['ok'] is True

    @patch('apps.retrieval.hybrid.hybrid_search')
    @patch('apps.knowledge.access.filter_accessible_doc_ids')
    def test_execute_when_unauthenticated_then_skips_permission_filter(self, mock_filter, mock_hybrid):
        """is_authenticated=False 的用户跳过权限过滤"""
        mock_hybrid.return_value = {
            'chunks': [_chunk(1, 101)],
            'stats': {},
        }
        tool = KnowledgeSearchTool()
        user = MagicMock(is_authenticated=False)
        ret = tool.execute(ToolContext(user=user), query='问题')
        mock_filter.assert_not_called()


class TestKnowledgeSearchTopK:
    """top_k 参数防御"""

    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_execute_when_top_k_exceeds_10_then_clamped(self, mock_hybrid):
        """top_k > 10 时截断到 10"""
        mock_hybrid.return_value = {
            'chunks': [_chunk(i, 100 + i) for i in range(1, 21)],
            'stats': {},
        }
        tool = KnowledgeSearchTool()
        ret = tool.execute(ToolContext(), query='问题', top_k=20)
        assert len(ret['meta']['chunks']) == 10

    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_execute_when_top_k_below_1_then_clamped_to_1(self, mock_hybrid):
        """top_k 为负数时通过 max(1, ...) 下限保护截断到 1

        注意：top_k=0 会触发 `0 or 5` 的 falsy 短路，落入默认值 5，
        因此使用 -1 来验证下限保护逻辑（max(1, min(-1, 10)) = 1）。
        """
        mock_hybrid.return_value = {
            'chunks': [_chunk(1, 101), _chunk(2, 102)],
            'stats': {},
        }
        tool = KnowledgeSearchTool()
        ret = tool.execute(ToolContext(), query='问题', top_k=-1)
        assert len(ret['meta']['chunks']) == 1

    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_execute_when_top_k_none_then_defaults_to_5(self, mock_hybrid):
        """top_k=None 时默认为 5"""
        mock_hybrid.return_value = {
            'chunks': [_chunk(i, 100 + i) for i in range(1, 11)],
            'stats': {},
        }
        tool = KnowledgeSearchTool()
        ret = tool.execute(ToolContext(), query='问题', top_k=None)
        assert len(ret['meta']['chunks']) == 5


class TestKnowledgeSearchFormatting:
    """结果格式化"""

    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_execute_when_long_content_then_truncated(self, mock_hybrid):
        """单片段内容超过 1500 字时截断"""
        long_content = 'x' * 2000
        mock_hybrid.return_value = {
            'chunks': [_chunk(1, 101, content=long_content)],
            'stats': {},
        }
        tool = KnowledgeSearchTool()
        ret = tool.execute(ToolContext(), query='问题')
        assert '...' in ret['result']
        assert len(ret['result']) < 2000

    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_execute_when_has_section_and_page_then_included_in_result(self, mock_hybrid):
        """结果文本包含章节与页码位置信息"""
        mock_hybrid.return_value = {
            'chunks': [_chunk(1, 101, title='手册', section='第一章', page=5)],
            'stats': {},
        }
        tool = KnowledgeSearchTool()
        ret = tool.execute(ToolContext(), query='问题')
        assert '第一章' in ret['result']
        assert '5' in ret['result']

    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_execute_when_no_section_or_page_then_omits_location(self, mock_hybrid):
        """片段无章节/页码时不输出位置信息"""
        mock_hybrid.return_value = {
            'chunks': [_chunk(1, 101, title='文档', section=None, page=None)],
            'stats': {},
        }
        tool = KnowledgeSearchTool()
        ret = tool.execute(ToolContext(), query='问题')
        assert '章节' not in ret['result']
        assert '页码' not in ret['result']

    @patch('apps.retrieval.hybrid.hybrid_search')
    def test_execute_when_multiple_chunks_then_numbered(self, mock_hybrid):
        """多个片段按序号 [1] [2] [3] 编号"""
        mock_hybrid.return_value = {
            'chunks': [
                _chunk(1, 101, title='A', content='内容A'),
                _chunk(2, 102, title='B', content='内容B'),
                _chunk(3, 103, title='C', content='内容C'),
            ],
            'stats': {},
        }
        tool = KnowledgeSearchTool()
        ret = tool.execute(ToolContext(), query='问题', top_k=10)
        assert '[1]' in ret['result']
        assert '[2]' in ret['result']
        assert '[3]' in ret['result']
