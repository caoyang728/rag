"""
agent.tools.web_search 单元测试

覆盖 WebSearchTool 全部分支：
- execute() 主流程：Tavily 成功 / Tavily 失败降级 DDG / 两个数据源都失败
- _search_tavily：无 API Key / 正常返回 / 含 answer 字段 / 网络异常
- _search_duckduckgo：库未安装 / 正常返回 / 无结果 / 异常
- max_results 参数防御

"""
import json

import pytest
from unittest.mock import patch, MagicMock

from apps.agent.tools.base import ToolContext
from apps.agent.tools.web_search import WebSearchTool

pytestmark = pytest.mark.unit


class TestWebSearchExecute:
    """execute() 主流程：数据源优先级与降级"""

    @patch.object(WebSearchTool, '_search_tavily')
    def test_execute_when_tavily_success_then_skips_duckduckgo(self, mock_tavily):
        """Tavily 成功时不再调用 DuckDuckGo"""
        mock_tavily.return_value = {
            'result': 'Tavily 结果', 'ok': True,
            'meta': {'results': [], 'source': 'tavily'},
        }
        tool = WebSearchTool()
        with patch.object(tool, '_search_duckduckgo') as mock_ddg:
            ret = tool.execute(ToolContext(), query='测试')
        mock_ddg.assert_not_called()
        assert ret['ok'] is True
        assert 'Tavily 结果' in ret['result']

    @patch.object(WebSearchTool, '_search_tavily')
    @patch.object(WebSearchTool, '_search_duckduckgo')
    def test_execute_when_tavily_fails_then_falls_back_to_ddg(self, mock_ddg, mock_tavily):
        """Tavily 失败时降级到 DuckDuckGo"""
        mock_tavily.return_value = {
            'result': 'Tavily 失败', 'ok': False,
            'meta': {'results': [], 'source': 'tavily'},
        }
        mock_ddg.return_value = {
            'result': 'DDG 结果', 'ok': True,
            'meta': {'results': [], 'source': 'duckduckgo'},
        }
        tool = WebSearchTool()
        ret = tool.execute(ToolContext(), query='测试')
        assert ret['ok'] is True
        assert 'DDG 结果' in ret['result']

    @patch.object(WebSearchTool, '_search_tavily')
    @patch.object(WebSearchTool, '_search_duckduckgo')
    def test_execute_when_both_fail_then_returns_error(self, mock_ddg, mock_tavily):
        """两个数据源都失败时返回统一错误"""
        mock_tavily.return_value = {
            'result': 'Tavily 错误', 'ok': False,
            'meta': {'results': [], 'source': 'tavily'},
        }
        mock_ddg.return_value = {
            'result': 'DDG 错误', 'ok': False,
            'meta': {'results': [], 'source': 'duckduckgo'},
        }
        tool = WebSearchTool()
        ret = tool.execute(ToolContext(), query='测试')
        assert ret['ok'] is False
        assert '联网搜索暂时不可用' in ret['result']
        assert 'Tavily 错误' in ret['result']
        assert 'DDG 错误' in ret['result']
        assert ret['meta']['source'] == 'none'

    def test_execute_when_max_results_exceeds_limit_then_clamped(self):
        """max_results 限制在 1-10 范围"""
        tool = WebSearchTool()
        with patch.object(tool, '_search_tavily') as mock_tavily:
            mock_tavily.return_value = {'result': 'ok', 'ok': True,
                                        'meta': {'results': [], 'source': 'tavily'}}
            tool.execute(ToolContext(), query='测试', max_results=100)
            # max_results 被截断到 10
            assert mock_tavily.call_args[0][1] == 10

    def test_execute_when_max_results_none_then_defaults_to_5(self):
        """max_results=None 时默认为 5"""
        tool = WebSearchTool()
        with patch.object(tool, '_search_tavily') as mock_tavily:
            mock_tavily.return_value = {'result': 'ok', 'ok': True,
                                        'meta': {'results': [], 'source': 'tavily'}}
            tool.execute(ToolContext(), query='测试', max_results=None)
            assert mock_tavily.call_args[0][1] == 5


class TestWebSearchTavily:
    """_search_tavily：Tavily API 搜索"""

    def test_search_tavily_when_no_api_key_then_returns_error(self):
        """未配置 TAVILY_API_KEY 时返回明确错误"""
        tool = WebSearchTool()
        with patch('django.conf.settings', MagicMock(TAVILY_API_KEY='')):
            ret = tool._search_tavily('查询', 5)
        assert ret['ok'] is False
        assert '未配置 TAVILY_API_KEY' in ret['result']
        assert ret['meta']['source'] == 'tavily'

    @patch('urllib.request.urlopen')
    def test_search_tavily_when_has_answer_then_includes_answer(self, mock_urlopen):
        """Tavily 返回 answer 字段时在结果中包含摘要"""
        api_response = {
            'answer': '这是摘要',
            'results': [
                {'title': '结果1', 'url': 'http://a.com', 'content': '内容1'},
                {'title': '结果2', 'url': 'http://b.com', 'content': '内容2'},
            ],
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_response).encode('utf-8')
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        tool = WebSearchTool()
        with patch('django.conf.settings', MagicMock(TAVILY_API_KEY='test_key')):
            ret = tool._search_tavily('查询', 5)
        assert ret['ok'] is True
        assert '摘要: 这是摘要' in ret['result']
        assert '结果1' in ret['result']
        assert 'http://a.com' in ret['result']

    @patch('urllib.request.urlopen')
    def test_search_tavily_when_no_answer_then_omits_answer(self, mock_urlopen):
        """Tavily 无 answer 字段时仅返回结果列表"""
        api_response = {
            'results': [{'title': '结果1', 'url': 'http://a.com', 'content': '内容1'}],
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_response).encode('utf-8')
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        tool = WebSearchTool()
        with patch('django.conf.settings', MagicMock(TAVILY_API_KEY='test_key')):
            ret = tool._search_tavily('查询', 5)
        assert ret['ok'] is True
        assert '结果1' in ret['result']

    @patch('urllib.request.urlopen')
    def test_search_tavily_when_empty_results_then_returns_no_match(self, mock_urlopen):
        """Tavily 返回空结果列表"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({'results': []}).encode('utf-8')
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        tool = WebSearchTool()
        with patch('django.conf.settings', MagicMock(TAVILY_API_KEY='test_key')):
            ret = tool._search_tavily('查询', 5)
        assert ret['ok'] is True
        assert '未找到相关结果' in ret['result']

    @patch('urllib.request.urlopen', side_effect=Exception('network error'))
    def test_search_tavily_when_network_error_then_returns_error(self, mock_urlopen):
        """Tavily 网络异常时返回错误信息"""
        tool = WebSearchTool()
        with patch('django.conf.settings', MagicMock(TAVILY_API_KEY='test_key')):
            ret = tool._search_tavily('查询', 5)
        assert ret['ok'] is False
        assert 'Tavily 调用失败' in ret['result']
        assert 'network error' in ret['result']


class TestWebSearchDuckDuckGo:
    """_search_duckduckgo：DuckDuckGo 兜底搜索"""

    def test_search_duckduckgo_when_not_installed_then_returns_error(self):
        """duckduckgo_search 库未安装时返回安装提示"""
        tool = WebSearchTool()
        with patch('builtins.__import__', side_effect=ImportError('no module')):
            ret = tool._search_duckduckgo('查询', 5)
        assert ret['ok'] is False
        assert 'DuckDuckGo 库未安装' in ret['result']
        assert ret['meta']['source'] == 'duckduckgo'

    def test_search_duckduckgo_when_normal_then_returns_results(self):
        """DuckDuckGo 正常返回结果"""
        tool = WebSearchTool()
        mock_ddgs = MagicMock()
        mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs.text.return_value = iter([
            {'title': '结果1', 'href': 'http://a.com', 'body': '内容1'},
            {'title': '结果2', 'url': 'http://b.com', 'content': '内容2'},
        ])

        mock_module = MagicMock()
        mock_module.DDGS = MagicMock(return_value=mock_ddgs)
        with patch.dict('sys.modules', {'duckduckgo_search': mock_module}):
            ret = tool._search_duckduckgo('查询', 5)
        assert ret['ok'] is True
        assert '结果1' in ret['result']
        assert 'http://a.com' in ret['result']

    def test_search_duckduckgo_when_no_results_then_returns_empty(self):
        """DuckDuckGo 无结果时返回未找到提示"""
        tool = WebSearchTool()
        mock_ddgs = MagicMock()
        mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs.text.return_value = iter([])

        mock_module = MagicMock()
        mock_module.DDGS = MagicMock(return_value=mock_ddgs)
        with patch.dict('sys.modules', {'duckduckgo_search': mock_module}):
            ret = tool._search_duckduckgo('查询', 5)
        assert ret['ok'] is True
        assert '未找到相关结果' in ret['result']

    def test_search_duckduckgo_when_exception_then_returns_error(self):
        """DuckDuckGo 调用异常时返回错误"""
        tool = WebSearchTool()
        mock_ddgs = MagicMock()
        mock_ddgs.__enter__ = MagicMock(side_effect=RuntimeError('ddg crashed'))
        mock_ddgs.__exit__ = MagicMock(return_value=False)

        mock_module = MagicMock()
        mock_module.DDGS = MagicMock(return_value=mock_ddgs)
        with patch.dict('sys.modules', {'duckduckgo_search': mock_module}):
            ret = tool._search_duckduckgo('查询', 5)
        assert ret['ok'] is False
        assert 'DuckDuckGo 调用失败' in ret['result']
        assert 'ddg crashed' in ret['result']
