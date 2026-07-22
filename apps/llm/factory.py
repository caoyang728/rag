"""
LLM Factory - 单 Provider 配置
单例 + 惰性初始化；业务代码只调 `get_llm().chat(...)`，不关心具体供应商
"""
from loguru import logger
from django.conf import settings

from .providers.base import BaseLLMProvider
from .providers.deepseek import DeepSeekProvider


_instances = {}


def get_llm(provider: str = None) -> BaseLLMProvider:
    """获取 LLM 实例；默认使用 deepseek"""
    provider = provider or 'deepseek'
    if provider in _instances:
        return _instances[provider]

    inst = DeepSeekProvider(
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        model=settings.LLM_MODEL,
        timeout=60,
    )
    _instances[provider] = inst
    logger.info('[LLM Factory] 初始化 %s model=%s', provider, inst.model)
    return inst


def reset_llm():
    """测试用：重置单例"""
    _instances.clear()
