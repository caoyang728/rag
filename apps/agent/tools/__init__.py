"""
Agent 工具包
提供工具注册表的统一入口，供 ReAct 循环调用。

用法：
    from apps.agent.tools import get_default_registry, ToolContext

    registry = get_default_registry()
    openai_tools = registry.to_openai_tools()  # 传给 LLM
    ctx = ToolContext(user=user, session=session, root_types=root_types)
    result = registry.execute('knowledge_search', {'query': '...'}, ctx)
"""
from .base import BaseTool, ToolContext, ToolRegistry, parse_tool_arguments
from .knowledge_search import KnowledgeSearchTool
from .web_search import WebSearchTool
from .calculator import CalculatorTool
from .text2sql import Text2SqlTool
from .wiki_search import WikiSearchTool
from .graph_search import GraphSearchTool


# 全局默认注册表（懒加载，避免 import 时即创建）
_default_registry: ToolRegistry = None


def get_default_registry() -> ToolRegistry:
    """获取默认工具注册表（单例）

    注册 6 个基础工具：
    - knowledge_search: 内部知识库检索
    - web_search: 联网搜索（Tavily + DuckDuckGo 兜底）
    - calculator: 精确数学计算
    - text2sql: 业务数据库查询
    - wiki_search: Wiki 知识页搜索（已整理的知识页面）
    - graph_search: 知识图谱实体关系检索
    """
    global _default_registry
    if _default_registry is None:
        _default_registry = ToolRegistry()
        _default_registry.register(KnowledgeSearchTool())
        _default_registry.register(WebSearchTool())
        _default_registry.register(CalculatorTool())
        _default_registry.register(Text2SqlTool())
        _default_registry.register(WikiSearchTool())
        _default_registry.register(GraphSearchTool())
    return _default_registry


def get_available_tool_names() -> list:
    """返回当前可用的工具名称列表

    供前端展示可用工具、配置接口等使用。
    根据环境变量动态判断：web_search 依赖 TAVILY_API_KEY/duckduckgo_search，
    但因有兜底机制，默认都返回。
    """
    return get_default_registry().names()


__all__ = [
    'BaseTool', 'ToolContext', 'ToolRegistry', 'parse_tool_arguments',
    'KnowledgeSearchTool', 'WebSearchTool', 'CalculatorTool', 'Text2SqlTool',
    'WikiSearchTool', 'GraphSearchTool',
    'get_default_registry', 'get_available_tool_names',
]
