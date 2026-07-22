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
        t0 = time.time()
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
            )
            latency_ms = int((time.time() - t0) * 1000)
            content = resp.choices[0].message.content or ''
            usage = resp.usage
            pt = getattr(usage, 'prompt_tokens', 0) if usage else 0
            ct = getattr(usage, 'completion_tokens', 0) if usage else 0
            tt = getattr(usage, 'total_tokens', pt + ct) if usage else pt + ct
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
            }
        except Exception as e:
            logger.exception('[DeepSeek] chat error')
            return {
                'content': f'[LLM 调用失败: {e.__class__.__name__}] {str(e)[:200]}',
                'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0,
                'latency_ms': int((time.time() - t0) * 1000),
                'model': self.model, 'provider': self.name, 'cost': 0,
                'finish_reason': 'error', 'error': str(e),
            }

    def stream(self, messages, temperature=0.3, max_tokens=2048, **kwargs) -> Iterator[Dict[str, Any]]:
        t0 = time.time()
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )
            full = []
            for chunk in resp:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content or ''
                if delta:
                    full.append(delta)
                    yield {'delta': delta, 'finish': False}
            latency_ms = int((time.time() - t0) * 1000)
            yield {
                'delta': '',
                'finish': True,
                'content': ''.join(full),
                'latency_ms': latency_ms,
                'model': self.model,
                'provider': self.name,
            }
        except Exception as e:
            logger.exception('[DeepSeek] stream error')
            yield {'delta': f'[流式失败: {e}]', 'finish': True, 'error': str(e)}
