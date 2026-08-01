"""
LLM Provider 抽象基类
LLM 适配层——业务代码不感知具体供应商，通过 factory 切换
支持：chat（同步）/ stream（SSE 流式）/ embed（向量化）/ rerank
"""
from abc import ABC, abstractmethod
from typing import Iterator, List, Dict, Any, Optional


class BaseLLMProvider(ABC):
    """所有 Provider 必须实现以下接口"""

    name: str = 'base'
    default_model: str = ''

    def __init__(self, api_key: str, base_url: str = '', model: str = '',
                 timeout: int = 60, **kwargs):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model or self.default_model
        self.timeout = timeout
        self.extra = kwargs

    @abstractmethod
    def chat(self, messages: List[Dict[str, str]],
             temperature: float = 0.3,
             max_tokens: int = 2048,
             **kwargs) -> Dict[str, Any]:
        """同步 chat

        通过 **kwargs 支持 function calling：
        - tools: List[Dict] — OpenAI tools schema（function definitions）
        - tool_choice: str | dict — 'auto' / 'none' / {'type': 'function', 'function': {'name': ...}}

        返回: {'content': str, 'prompt_tokens': int, 'completion_tokens': int,
              'total_tokens': int, 'latency_ms': int, 'model': str, 'cost': float,
              'finish_reason': str,
              'tool_calls': List[{'id': str, 'name': str, 'arguments': str}] | []}
        当 LLM 决定调用工具时 content 为空、tool_calls 非空、finish_reason='tool_calls'。
        """
        raise NotImplementedError

    @abstractmethod
    def stream(self, messages: List[Dict[str, str]],
               temperature: float = 0.3,
               max_tokens: int = 2048,
               **kwargs) -> Iterator[Dict[str, Any]]:
        """流式 chat

        通过 **kwargs 支持 function calling（同 chat）。

        yield 帧:
        - {'delta': str, 'finish': False}              # 文本增量
        - {'delta': '', 'finish': True, 'content': str,
           'tool_calls': [...] | [], 'finish_reason': str, ...}  # 结束帧
        当 LLM 决定调用工具时，整个过程不产生文本 delta，结束帧的 tool_calls 非空、
        finish_reason='tool_calls'，由上层 ReAct 循环执行工具后再次发起流式调用。
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 工具调用相关辅助方法（OpenAI 兼容协议通用，子类可直接复用）
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_tool_calls(message) -> List[Dict[str, str]]:
        """从 OpenAI 同步响应的 message 对象中提取 tool_calls

        将 OpenAI SDK 的 ToolCall 对象统一转为 dict 结构，便于上层处理。
        arguments 是 JSON 字符串（OpenAI 协议规定），由调用方自行 json.loads。

        Args:
            message: OpenAI ChatCompletionMessage 对象（含 tool_calls 属性）

        Returns:
            [{'id': str, 'name': str, 'arguments': str}]，无工具调用时返回空列表
        """
        tool_calls = getattr(message, 'tool_calls', None) or []
        result = []
        for tc in tool_calls:
            fn = getattr(tc, 'function', None)
            if not fn:
                continue
            result.append({
                'id': getattr(tc, 'id', '') or '',
                'name': getattr(fn, 'name', '') or '',
                'arguments': getattr(fn, 'arguments', '') or '',
            })
        return result

    @staticmethod
    def _merge_tool_call_deltas(accumulated: List[Dict], delta_obj) -> List[Dict]:
        """累积流式响应中的 tool_calls 分片

        OpenAI 流式协议下，tool_calls 按 index 分片返回：
        - 首帧包含 id 和 name，arguments 可能为空或部分
        - 后续帧只补充 arguments 片段（id/name 为 None）
        需要按 index 累积拼接 arguments 字符串。

        Args:
            accumulated: 已累积的 tool_calls 列表（按 index 顺序）
            delta_obj: 当前帧的 delta.tool_calls（OpenAI ChoiceDeltaToolCall 列表）

        Returns:
            更新后的累积列表
        """
        if not delta_obj:
            return accumulated
        for tc_delta in delta_obj:
            idx = getattr(tc_delta, 'index', None)
            if idx is None:
                continue
            # 按 index 扩展列表
            while len(accumulated) <= idx:
                accumulated.append({'id': '', 'name': '', 'arguments': ''})
            cur = accumulated[idx]
            # 首帧带 id 和 name
            tc_id = getattr(tc_delta, 'id', None)
            if tc_id:
                cur['id'] = tc_id
            fn = getattr(tc_delta, 'function', None)
            if fn:
                name = getattr(fn, 'name', None)
                if name:
                    cur['name'] = name
                args_chunk = getattr(fn, 'arguments', None)
                if args_chunk:
                    cur['arguments'] += args_chunk
        return accumulated

    def embed(self, texts: List[str], model: Optional[str] = None) -> List[List[float]]:
        """默认走 embedding.py，Provider 可选覆盖"""
        raise NotImplementedError

    def rerank(self, query: str, docs: List[str], top_k: int = 5,
               model: Optional[str] = None) -> List[Dict[str, Any]]:
        """默认走 rerank，Provider 可选覆盖，返回 [{index, score}]"""
        raise NotImplementedError
