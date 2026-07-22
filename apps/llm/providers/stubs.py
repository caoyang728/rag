"""
其他 Provider stub —— 提供接口占位，未来私有化时替换
所有 stub 都保持 OpenAI 兼容协议，实际调用直接复用 openai SDK 走对应 base_url
"""
import time
from loguru import logger
from typing import Iterator, Dict, Any

from openai import OpenAI
from .base import BaseLLMProvider



class _OpenAICompatibleProvider(BaseLLMProvider):
    """OpenAI 兼容协议的通用父类；子类只需要重写 name / default_model / default_base_url"""

    default_base_url = ''

    def __init__(self, api_key: str, base_url: str = '', model: str = '',
                 timeout: int = 60, **kwargs):
        base_url = base_url or self.default_base_url
        super().__init__(api_key, base_url, model, timeout, **kwargs)
        self.client = OpenAI(api_key=api_key or 'sk-stub', base_url=base_url, timeout=timeout)

    def chat(self, messages, temperature=0.3, max_tokens=2048, **kwargs):
        t0 = time.time()
        try:
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages,
                temperature=temperature, max_tokens=max_tokens, stream=False,
            )
            latency_ms = int((time.time() - t0) * 1000)
            content = resp.choices[0].message.content or ''
            usage = resp.usage
            pt = getattr(usage, 'prompt_tokens', 0) if usage else 0
            ct = getattr(usage, 'completion_tokens', 0) if usage else 0
            return {'content': content, 'prompt_tokens': pt, 'completion_tokens': ct,
                    'total_tokens': pt + ct, 'latency_ms': latency_ms,
                    'model': self.model, 'provider': self.name, 'cost': 0,
                    'finish_reason': resp.choices[0].finish_reason}
        except Exception as e:
            logger.exception('[%s] chat error', self.name)
            return {'content': f'[{self.name} 调用失败: {e}]', 'prompt_tokens': 0,
                    'completion_tokens': 0, 'total_tokens': 0,
                    'latency_ms': int((time.time() - t0) * 1000),
                    'model': self.model, 'provider': self.name, 'cost': 0,
                    'finish_reason': 'error', 'error': str(e)}

    def stream(self, messages, temperature=0.3, max_tokens=2048, **kwargs) -> Iterator[Dict[str, Any]]:
        t0 = time.time()
        try:
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages,
                temperature=temperature, max_tokens=max_tokens, stream=True,
            )
            full = []
            for chunk in resp:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content or ''
                if delta:
                    full.append(delta)
                    yield {'delta': delta, 'finish': False}
            yield {'delta': '', 'finish': True, 'content': ''.join(full),
                   'latency_ms': int((time.time() - t0) * 1000),
                   'model': self.model, 'provider': self.name}
        except Exception as e:
            logger.exception('[%s] stream error', self.name)
            yield {'delta': f'[流式失败: {e}]', 'finish': True, 'error': str(e)}


class QwenProvider(_OpenAICompatibleProvider):
    """通义千问 - DashScope 兼容 OpenAI 协议"""
    name = 'qwen'
    default_model = 'qwen-plus'
    default_base_url = 'https://dashscope.aliyuncs.com/compatible-mode/v1'


class GlmProvider(_OpenAICompatibleProvider):
    """智谱 GLM"""
    name = 'glm'
    default_model = 'glm-4-plus'
    default_base_url = 'https://open.bigmodel.cn/api/paas/v4'


class VllmProvider(_OpenAICompatibleProvider):
    """vLLM 自建推理服务（私有化部署）"""
    name = 'vllm'
    default_model = 'qwen2.5-14b-instruct'
    default_base_url = 'http://vllm:8000/v1'


class OllamaProvider(_OpenAICompatibleProvider):
    """Ollama 本地推理（开发测试用）"""
    name = 'ollama'
    default_model = 'qwen2.5:7b'
    default_base_url = 'http://ollama:11434/v1'
