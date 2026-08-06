"""
agent.tools 包入口单元测试

覆盖 tools/__init__.py 的：
- get_default_registry：单例创建（首次调用注册 6 个工具）+ 二次调用返回同一实例
- get_available_tool_names：委托 registry.names() 返回工具名列表
- __all__ 导出符号完整

纯逻辑测试，不依赖 DB。需重置模块级单例 _default_registry 以触发创建分支。
"""
import pytest

import apps.agent.tools as tools_pkg
from apps.agent.tools import (
    get_default_registry, get_available_tool_names,
    BaseTool, ToolContext, ToolRegistry, parse_tool_arguments,
    KnowledgeSearchTool, WebSearchTool, CalculatorTool, Text2SqlTool,
    WikiSearchTool, GraphSearchTool,
)

pytestmark = pytest.mark.unit


# ============================================================================
# get_default_registry：单例 + 6 工具注册
# ============================================================================
class TestGetDefaultRegistry:
    """get_default_registry 单例与工具注册"""

    @pytest.fixture(autouse=True)
    def _reset_singleton(self, monkeypatch):
        """每个测试前重置模块级单例，确保触发创建分支

        _default_registry 是模块级全局变量，单例模式下首次调用才会注册工具；
        重置后可重复验证创建逻辑（覆盖 __init__.py 第 38-46 行）。
        """
        monkeypatch.setattr(tools_pkg, '_default_registry', None)

    def test_get_default_registry_when_first_call_then_creates_with_six_tools(self):
        """首次调用：创建注册表并注册 6 个工具"""
        registry = get_default_registry()
        assert registry is not None
        names = set(registry.names())
        # 6 个内置工具：knowledge_search / web_search / calculator / text2sql / wiki_search / graph_search
        assert names == {
            'knowledge_search', 'web_search', 'calculator',
            'text2sql', 'wiki_search', 'graph_search',
        }

    def test_get_default_registry_when_second_call_then_returns_same_instance(self):
        """二次调用：返回同一实例（单例）"""
        first = get_default_registry()
        second = get_default_registry()
        assert first is second

    def test_get_default_registry_then_instances_are_correct_types(self):
        """注册的工具实例类型正确"""
        registry = get_default_registry()
        assert isinstance(registry.get('knowledge_search'), KnowledgeSearchTool)
        assert isinstance(registry.get('web_search'), WebSearchTool)
        assert isinstance(registry.get('calculator'), CalculatorTool)
        assert isinstance(registry.get('text2sql'), Text2SqlTool)
        assert isinstance(registry.get('wiki_search'), WikiSearchTool)
        assert isinstance(registry.get('graph_search'), GraphSearchTool)

    def test_get_default_registry_then_to_openai_tools_returns_six_schemas(self):
        """导出的 OpenAI tools schema 数量为 6"""
        registry = get_default_registry()
        tools = registry.to_openai_tools()
        assert len(tools) == 6
        # 每个工具 schema 符合 OpenAI 协议
        for t in tools:
            assert t['type'] == 'function'
            assert 'name' in t['function']
            assert 'description' in t['function']
            assert 'parameters' in t['function']


# ============================================================================
# get_available_tool_names：委托 registry.names()
# ============================================================================
class TestGetAvailableToolNames:
    """get_available_tool_names 返回工具名列表"""

    def test_get_available_tool_names_then_returns_six_names(self, monkeypatch):
        """返回 6 个工具名（覆盖 __init__.py 第 56 行）"""
        # 重置单例确保走创建路径
        monkeypatch.setattr(tools_pkg, '_default_registry', None)
        names = get_available_tool_names()
        assert len(names) == 6
        assert 'knowledge_search' in names
        assert 'calculator' in names
        assert 'graph_search' in names

    def test_get_available_tool_names_then_delegates_to_registry(self, monkeypatch):
        """get_available_tool_names 应委托 registry.names()"""
        # 用 mock registry 验证委托关系
        mock_registry = type('MockRegistry', (), {'names': lambda self: ['a', 'b']})()
        monkeypatch.setattr(tools_pkg, '_default_registry', mock_registry)
        assert get_available_tool_names() == ['a', 'b']


# ============================================================================
# __all__ 导出完整性
# ============================================================================
class TestToolsAllExports:
    """__all__ 应包含所有公开符号"""

    def test_all_exports_then_all_present(self):
        """__all__ 中的符号都能从包导入"""
        expected = {
            'BaseTool', 'ToolContext', 'ToolRegistry', 'parse_tool_arguments',
            'KnowledgeSearchTool', 'WebSearchTool', 'CalculatorTool', 'Text2SqlTool',
            'WikiSearchTool', 'GraphSearchTool',
            'get_default_registry', 'get_available_tool_names',
        }
        assert set(tools_pkg.__all__) == expected

    def test_all_exports_then_symbols_importable(self):
        """__all__ 中每个符号都能在包命名空间中找到"""
        for name in tools_pkg.__all__:
            assert hasattr(tools_pkg, name), f'{name} 未在 apps.agent.tools 中定义'
