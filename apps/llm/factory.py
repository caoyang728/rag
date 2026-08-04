"""
LLM Factory - 支持双模型配置
单例 + 惰性初始化；业务代码可选择使用默认模型（基础）或高级模型
- get_llm(): 获取默认模型（基础模型，用于简单任务）
- get_llm_advanced(): 获取高级模型（用于复杂任务）

配置来源：
- model_name：SystemConfig.LLM_BASE_MODEL / LLM_ADVANCED_MODEL
- base_url / timeout / provider：LLMModel 表按 model_name 反查
- api_key：从 env 读取（敏感凭证不入库）

DB 未配置时的降级策略：
- model_name 缺失：抛 ValueError，由调用方决定是否降级
- LLMModel 未命中：记录 warning，base_url 留空

缓存：config_loader 内部 5min TTL，模型管理改动后自动失效，无需重启进程。
"""
from loguru import logger
from django.conf import settings

from .providers.base import BaseLLMProvider
from .providers.deepseek import DeepSeekProvider


# 单例缓存：key=model_name，value=LLMProvider 实例
_instances = {}


def get_llm(model: str = None) -> BaseLLMProvider:
    """获取 LLM 实例
    - 默认使用基础模型（LLM_BASE_MODEL），适合简单任务
    - 可通过 model 参数指定特定模型

    配置读取：
    1) model_name：SystemConfig.LLM_BASE_MODEL
    2) base_url/timeout/provider：LLMModel 表按 model_name 反查
    3) api_key：从 env 读取
    """
    if not model:
        from apps.system.config_loader import get_config_value
        model = get_config_value('LLM_BASE_MODEL', default='', value_type='string')

    if not model:
        raise ValueError('SystemConfig.LLM_BASE_MODEL 未配置，请在系统配置页面设置基础模型')

    if model in _instances:
        return _instances[model]

    api_key, base_url, timeout, provider = _resolve_llm_params(model)

    inst = DeepSeekProvider(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout=timeout,
    )
    _instances[model] = inst
    logger.info(f'[LLM Factory] 初始化 model={inst.model} base_url={base_url} timeout={timeout} provider={provider or "(未配置)"}')
    return inst


def get_llm_advanced() -> BaseLLMProvider:
    """获取高级模型（LLM_ADVANCED_MODEL），适合复杂任务"""
    from apps.system.config_loader import get_config_value
    advanced_model = get_config_value('LLM_ADVANCED_MODEL', default='', value_type='string')
    if not advanced_model:
        logger.warning('[LLM Factory] LLM_ADVANCED_MODEL 未配置，回退到基础模型')
        return get_llm()
    return get_llm(advanced_model)


def reset_llm():
    """测试用：重置单例

    业务侧一般不主动调用：配置变更后依赖 config_loader 5min TTL 兜底，
    若需立即生效可在系统配置页面手动重启 worker 或调用此方法。
    """
    _instances.clear()


def _resolve_llm_params(model_name: str):
    """从 LLMModel 表拼装 LLM 调用所需参数

    Returns: (api_key, base_url, timeout, provider)
    - api_key 从 env 取（敏感凭证不入库）
    - base_url / timeout / provider 从 LLMModel 读取
    - LLMModel 未命中时 base_url 留空、timeout 回退 SystemConfig.LLM_TIMEOUT 或 60
    """
    from apps.system.config_loader import get_llm_model_config, get_config_value

    api_key = getattr(settings, 'LLM_API_KEY', '') or ''

    llm_row = get_llm_model_config(model_name, model_type='llm')
    if llm_row:
        base_url = llm_row.get('base_url') or ''
        timeout = llm_row.get('timeout')
        if not timeout:
            timeout = get_config_value('LLM_TIMEOUT', default=60, value_type='int')
        provider = llm_row.get('provider', '')
        return api_key, base_url, int(timeout), provider

    base_url = ''
    timeout = get_config_value('LLM_TIMEOUT', default=60, value_type='int')
    logger.warning(f'[LLM Factory] LLMModel 表未配置 model={model_name}，base_url 将为空，请在模型管理中添加')
    return api_key, base_url, int(timeout), ''
