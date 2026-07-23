"""
LLM Factory - 支持双模型配置
单例 + 惰性初始化；业务代码可选择使用默认模型（基础）或高级模型
- get_llm(): 获取默认模型（基础模型，用于简单任务）
- get_llm_advanced(): 获取高级模型（用于复杂任务）
"""
from loguru import logger
from django.conf import settings

from .providers.base import BaseLLMProvider
from .providers.deepseek import DeepSeekProvider
from rag_project.config import LLMConfig


_instances = {}


def get_llm(model: str = None) -> BaseLLMProvider:
    """获取 LLM 实例
    - 默认使用基础模型（LLM_BASE_MODEL），适合简单任务
    - 可通过 model 参数指定特定模型
    """
    model_key = model or settings.LLM_BASE_MODEL
    if model_key in _instances:
        return _instances[model_key]

    inst = DeepSeekProvider(
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        model=model_key,
        timeout=LLMConfig.timeout(),
    )
    _instances[model_key] = inst
    logger.info('[LLM Factory] 初始化 model=%s', inst.model)
    return inst


def get_llm_advanced() -> BaseLLMProvider:
    """获取高级模型（LLM_ADVANCED_MODEL），适合复杂任务"""
    return get_llm(settings.LLM_ADVANCED_MODEL)


def reset_llm():
    """测试用：重置单例"""
    _instances.clear()
