"""
Agent 工具基类与注册表
- BaseTool：所有工具的抽象基类，定义 execute / to_openai_tool 接口
- ToolContext：工具执行上下文，封装 user/session 等运行时信息（用于权限过滤等）
- ToolRegistry：工具注册表，管理工具实例、按名称查找、导出 OpenAI tools schema
"""
import json
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class ToolContext:
    """工具执行上下文

    封装运行时信息，供工具在执行时访问用户身份、会话、检索范围等。
    knowledge_search 等工具需要 user 做权限过滤；text2sql 等工具不需要。
    """

    def __init__(self, user=None, session=None, root_types: list = None,
                 node_ids: list = None, llm=None):
        self.user = user
        self.session = session
        self.root_types = root_types
        self.node_ids = node_ids
        self.llm = llm  # 部分工具（如 text2sql）需要 LLM 生成 SQL，复用上层 LLM 实例


class BaseTool(ABC):
    """所有 Agent 工具的抽象基类

    子类必须定义：
    - name: 工具名称（与 OpenAI function name 一致，仅字母数字下划线）
    - description: 工具描述（LLM 据此决定是否调用）
    - parameters: OpenAI function parameters JSON Schema
    - execute(): 实际执行逻辑
    """

    name: str = ''
    description: str = ''
    parameters: Dict[str, Any] = {}

    @abstractmethod
    def execute(self, ctx: ToolContext, **kwargs) -> Dict[str, Any]:
        """执行工具

        Args:
            ctx: 工具执行上下文（user/session 等）
            **kwargs: 工具参数（由 LLM 生成的 arguments JSON 解析而来）

        Returns:
            {'result': str, 'ok': bool, 'meta': dict}
            - result: 工具执行结果文本（将作为 tool message 回填给 LLM）
            - ok: 是否成功（失败时 LLM 可据此重试或调整）
            - meta: 附加元信息（如检索命中的 chunk_ids，用于后续引用溯源）
        """
        raise NotImplementedError

    def to_openai_tool(self) -> Dict[str, Any]:
        """转换为 OpenAI tools schema 格式"""
        return {
            'type': 'function',
            'function': {
                'name': self.name,
                'description': self.description,
                'parameters': self.parameters,
            }
        }


class ToolRegistry:
    """工具注册表

    - register(): 注册工具实例
    - get(): 按名称获取工具
    - to_openai_tools(): 导出 OpenAI tools schema 列表（供 LLM 调用）
    - execute(): 按名称执行工具，统一异常处理与耗时记录
    """

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> 'ToolRegistry':
        self._tools[tool.name] = tool
        return self

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def to_openai_tools(self, names: List[str] = None) -> List[Dict[str, Any]]:
        """导出 OpenAI tools schema 列表

        Args:
            names: 指定工具名称列表；None 表示导出全部
        """
        if names is None:
            names = list(self._tools.keys())
        return [self._tools[n].to_openai_tool()
                for n in names if n in self._tools]

    def execute(self, name: str, arguments: Dict[str, Any],
                ctx: ToolContext) -> Dict[str, Any]:
        """执行工具，统一异常处理 + 耗时记录

        Args:
            name: 工具名称
            arguments: 工具参数（已从 JSON 解析为 dict）
            ctx: 执行上下文

        Returns:
            {'result': str, 'ok': bool, 'meta': dict, 'latency_ms': int, 'tool_name': str}
            失败时 result 包含错误信息，ok=False。
        """
        tool = self.get(name)
        if not tool:
            return {'result': f'工具 {name} 不存在', 'ok': False,
                    'meta': {}, 'latency_ms': 0, 'tool_name': name}
        t0 = time.time()
        try:
            ret = tool.execute(ctx, **arguments)
            ret.setdefault('meta', {})
            ret['latency_ms'] = int((time.time() - t0) * 1000)
            ret['tool_name'] = name
            return ret
        except Exception as e:
            from loguru import logger
            logger.exception(f'[ToolRegistry] tool {name} execute error')
            return {
                'result': f'工具执行失败: {e.__class__.__name__}: {str(e)[:300]}',
                'ok': False,
                'meta': {},
                'latency_ms': int((time.time() - t0) * 1000),
                'tool_name': name,
            }


def parse_tool_arguments(raw: str) -> Dict[str, Any]:
    """解析 LLM 返回的 tool_call arguments 字符串为 dict

    OpenAI 协议规定 arguments 是 JSON 字符串，但模型偶尔会输出带 ```json 包裹
    或首尾多余字符，这里做容错解析。

    Args:
        raw: LLM 返回的 arguments JSON 字符串

    Returns:
        解析后的 dict；解析失败时返回空 dict（避免阻断 Agent 循环）
    """
    if not raw:
        return {}
    s = raw.strip()
    # 兼容 ```json ... ``` 包裹
    if s.startswith('```'):
        s = s.strip('`')
        if s.lower().startswith('json'):
            s = s[4:]
        s = s.strip()
    try:
        return json.loads(s)
    except Exception:
        # 最后尝试：截取第一个 { 到最后一个 }
        try:
            start = s.find('{')
            end = s.rfind('}')
            if start != -1 and end != -1 and end > start:
                return json.loads(s[start:end + 1])
        except Exception:
            pass
        return {}
