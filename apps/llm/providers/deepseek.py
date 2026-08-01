"""
DeepSeek Provider - OpenAI 兼容协议
使用 openai SDK 走 base_url=https://api.deepseek.com，
完全兼容 chat completions API；成本按官方定价估算
"""
import time
from loguru import logger
from typing import Iterator, List, Dict, Any

from openai import OpenAI

from .base import BaseLLMProvider


# 官方定价（元 / 1K tokens），2025 年最新价
DEEPSEEK_PRICING = {
    'deepseek-chat': {'prompt': 0.002, 'completion': 0.008},
    'deepseek-reasoner': {'prompt': 0.004, 'completion': 0.016},
}


class DeepSeekProvider(BaseLLMProvider):
    name = 'deepseek'
    default_model = 'deepseek-chat'

    def __init__(self, api_key: str, base_url: str = 'https://api.deepseek.com/v1',
                 model: str = 'deepseek-chat', timeout: int = 60, **kwargs):
        super().__init__(api_key, base_url, model, timeout, **kwargs)
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def _estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        p = DEEPSEEK_PRICING.get(self.model, DEEPSEEK_PRICING['deepseek-chat'])
        return round((prompt_tokens * p['prompt'] + completion_tokens * p['completion']) / 1000, 6)

    def chat(self, messages, temperature=0.3, max_tokens=2048, **kwargs):
        """同步 chat，支持 function calling

        kwargs 中可传：
        - tools: OpenAI tools schema 列表，启用工具调用
        - tool_choice: 'auto' / 'none' / 指定函数；默认不传由 API 决策

        当 LLM 决定调用工具时，返回的 content 为空、tool_calls 非空、
        finish_reason='tool_calls'，由上层 ReAct 循环执行工具后回填结果再次调用。
        """
        t0 = time.time()
        tools = kwargs.get('tools')
        tool_choice = kwargs.get('tool_choice')
        try:
            request_kwargs = dict(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
            )
            # 仅当传入 tools 时才透传，避免无 tools 场景触发 API 参数校验
            if tools:
                request_kwargs['tools'] = tools
                if tool_choice is not None:
                    request_kwargs['tool_choice'] = tool_choice
            resp = self.client.chat.completions.create(**request_kwargs)
            latency_ms = int((time.time() - t0) * 1000)
            message = resp.choices[0].message
            content = message.content or ''
            usage = resp.usage
            pt = getattr(usage, 'prompt_tokens', 0) if usage else 0
            ct = getattr(usage, 'completion_tokens', 0) if usage else 0
            tt = getattr(usage, 'total_tokens', pt + ct) if usage else pt + ct
            # 提取工具调用（无工具调用时为空列表），统一为 dict 结构
            tool_calls = self._extract_tool_calls(message)
            return {
                'content': content,
                'prompt_tokens': pt,
                'completion_tokens': ct,
                'total_tokens': tt,
                'latency_ms': latency_ms,
                'model': self.model,
                'provider': self.name,
                'cost': self._estimate_cost(pt, ct),
                'finish_reason': resp.choices[0].finish_reason,
                'tool_calls': tool_calls,
            }
        except Exception as e:
            logger.exception('[DeepSeek] chat error')
            return {
                'content': f'[LLM 调用失败: {e.__class__.__name__}] {str(e)[:200]}',
                'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0,
                'latency_ms': int((time.time() - t0) * 1000),
                'model': self.model, 'provider': self.name, 'cost': 0,
                'finish_reason': 'error', 'error': str(e),
                'tool_calls': [],
            }

    def stream(self, messages, temperature=0.3, max_tokens=2048, **kwargs) -> Iterator[Dict[str, Any]]:
        """流式生成，yield {'delta', 'finish'} 帧

        支持流式 function calling：
        - 当 LLM 决定调用工具时，整个过程不产生文本 delta，
          结束帧的 tool_calls 非空、finish_reason='tool_calls'。
        - 当 LLM 直接生成文本时，正常 yield 文本 delta，结束帧 tool_calls 为空。

        使用 with 上下文管理器管理 Stream 生命周期：当调用方中断迭代（如客户端断开
        触发 GeneratorExit）时，with 会自动关闭底层 HTTP 连接，避免连接泄漏。
        """
        t0 = time.time()
        tools = kwargs.get('tools')
        tool_choice = kwargs.get('tool_choice')
        try:
            # WARNING: 必须用 with 管理 Stream 对象。
            # 当 ask_stream 生成器被 close()（客户端断开）时，GeneratorExit 会中断
            # for 循环，with 的 __exit__ 会调用 resp.close() 释放 HTTP 连接。
            request_kwargs = dict(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )
            if tools:
                request_kwargs['tools'] = tools
                if tool_choice is not None:
                    request_kwargs['tool_choice'] = tool_choice
            with self.client.chat.completions.create(**request_kwargs) as resp:
                full = []
                # 流式 tool_calls 分片累积（按 index 拼接 arguments）
                accumulated_tool_calls = []
                finish_reason = None
                for chunk in resp:
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    # 记录结束原因（最后一帧才有）
                    if getattr(choice, 'finish_reason', None):
                        finish_reason = choice.finish_reason
                    delta = choice.delta
                    # 文本增量
                    text_delta = getattr(delta, 'content', None) or ''
                    if text_delta:
                        full.append(text_delta)
                        yield {'delta': text_delta, 'finish': False}
                    # 工具调用分片累积（与文本增量互斥，OpenAI 协议保证）
                    tc_delta = getattr(delta, 'tool_calls', None)
                    if tc_delta:
                        accumulated_tool_calls = self._merge_tool_call_deltas(
                            accumulated_tool_calls, tc_delta)
                latency_ms = int((time.time() - t0) * 1000)
                yield {
                    'delta': '',
                    'finish': True,
                    'content': ''.join(full),
                    'latency_ms': latency_ms,
                    'model': self.model,
                    'provider': self.name,
                    'tool_calls': accumulated_tool_calls,
                    'finish_reason': finish_reason or 'stop',
                }
        except GeneratorExit:
            # 客户端断开：with 已自动关闭 resp，无需额外处理
            logger.info('[DeepSeek] stream interrupted by client disconnect')
            raise
        except Exception as e:
            logger.exception('[DeepSeek] stream error')
            yield {'delta': f'[流式失败: {e}]', 'finish': True, 'error': str(e),
                   'tool_calls': [], 'finish_reason': 'error'}
