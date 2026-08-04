"""系统配置统一读取入口（DB 存储 + 5min TTL 缓存）

设计要点：
- 配置来源为 SystemConfig / LLMModel 表；API Key 例外从 env 读取（敏感凭证不入库）。
- 缓存策略：5min TTL + 写后延迟双删。读路径走缓存，写路径同步删一次 + 异步延迟再删一次，
  折中解决"写后立即读仍命中旧缓存"与"DB 事务未提交就清缓存导致回填旧值"两类问题。
- 失败降级：DB 异常时返回调用方传入的 default，保证业务可用性优先于配置实时性。
- 类型转换：按 SystemConfig.value_type 转换为 Python 类型，避免调用方重复处理。

使用示例：
    from apps.system.config_loader import get_config_value, get_llm_model_config
    timeout = get_config_value('LLM_TIMEOUT', default=60, value_type='int')
    llm_cfg = get_llm_model_config('deepseek-chat')  # {'base_url': ..., 'timeout': ..., ...}
"""
import json
import threading
import time
from typing import Any, Optional, Dict

from django.core.cache import cache
from loguru import logger


# 缓存 TTL：5 分钟，平衡配置实时性与 DB 压力
_CACHE_TTL = 300

# 缓存 key 前缀，便于批量清理与隔离命名空间
_CFG_KEY_PREFIX = 'sys:cfg:'
_LLM_KEY_PREFIX = 'sys:llm:'


def get_config_value(key: str, default: Any = None, value_type: str = 'string') -> Any:
    """读取 SystemConfig 配置值（DB + 缓存）

    Args:
        key: 配置项 key，如 'LLM_TIMEOUT'
        default: DB 中未找到或为空时的兜底值（由调用方按业务合理默认传入）
        value_type: 期望类型 string/int/float/bool/json，按 SystemConfig.value_type 转换
                    传 None 表示不转换，原样返回字符串
    Returns:
        按指定类型转换后的值；DB 未找到或为空则返回 default
    """
    cache_key = _CFG_KEY_PREFIX + key
    try:
        cached = cache.get(cache_key)
        if cached is not None:
            return _cast_value(cached, value_type)
    except Exception as e:
        logger.warning(f'[config_loader] 读缓存失败 key={key}: {e}')

    raw_value, db_value_type = _read_config_from_db(key)
    if raw_value is not None and raw_value != '':
        effective_type = value_type or db_value_type
        try:
            cache.set(cache_key, raw_value, _CACHE_TTL)
        except Exception as e:
            logger.warning(f'[config_loader] 写缓存失败 key={key}: {e}')
        return _cast_value(raw_value, effective_type)

    return default


def get_llm_model_config(model_name: str, model_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """按 model_name 查询 LLMModel 配置（带缓存）

    用于业务侧拿到一个 model_name（如 'deepseek-chat'）后，查其 base_url / timeout 等。
    同名模型在 LLMModel 表中理论上唯一（无 unique 约束但业务约定不重名），故取第一条 active。

    Args:
        model_name: 模型标识，如 'deepseek-chat'
        model_type: 可选限定 'llm'/'embedding'/'rerank'，避免同名跨类型误匹配
    Returns:
        {'id', 'name', 'provider', 'model_type', 'base_url', 'model_name', 'timeout', 'is_active'}
        未找到时返回 None，由调用方决定兜底策略
    """
    cache_key = _LLM_KEY_PREFIX + (f'{model_type}:{model_name}' if model_type else model_name)
    try:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    except Exception as e:
        logger.warning(f'[config_loader] 读 LLM 缓存失败 model={model_name}: {e}')

    data = _read_llm_model_from_db(model_name, model_type)
    if data is not None:
        try:
            cache.set(cache_key, data, _CACHE_TTL)
        except Exception as e:
            logger.warning(f'[config_loader] 写 LLM 缓存失败 model={model_name}: {e}')
    return data


def get_llm_config_by_system_key(system_key: str) -> Dict[str, Any]:
    """便捷方法：根据 SystemConfig 中的模型 key 拼装完整 LLM 调用配置

    适用于 factory/embedding 等业务侧：传入 'LLM_BASE_MODEL' / 'EMBEDDING_MODEL' / 'RERANK_MODEL'
    自动获取对应的 model_name，再从 LLMModel 表反查 base_url / timeout。
    API Key 例外从 env 读取（敏感凭证不入库）。

    Returns:
        {
            'model_name': str,           # 实际模型标识
            'base_url': str,             # LLMModel.base_url
            'timeout': int,              # LLMModel.timeout 优先，None 时回退 SystemConfig.LLM_TIMEOUT，再回退 60
            'api_key': str,              # 仅 env 提供（API Key 不入 DB）
            'provider': str,             # LLMModel.provider（可能为空）
            'source': 'db' | 'missing',  # 标识本次配置来源，便于排障
        }
    """
    model_name = get_config_value(system_key, default='', value_type='string') or ''
    if not model_name:
        return {'model_name': '', 'base_url': '', 'timeout': 60, 'api_key': '',
                'provider': '', 'source': 'missing'}

    model_type_map = {
        'LLM_BASE_MODEL': 'llm', 'LLM_ADVANCED_MODEL': 'llm', 'EVAL_MODEL': 'llm',
        'EMBEDDING_MODEL': 'embedding', 'RERANK_MODEL': 'rerank',
    }
    model_type = model_type_map.get(system_key)
    llm_row = get_llm_model_config(model_name, model_type=model_type) if model_type else None

    api_key = _read_api_key_from_env(system_key)

    if llm_row:
        base_url = llm_row.get('base_url') or ''
        timeout = llm_row.get('timeout')
        if not timeout:
            timeout = get_config_value('LLM_TIMEOUT', default=60, value_type='int')
        return {
            'model_name': model_name,
            'base_url': base_url,
            'timeout': int(timeout),
            'api_key': api_key,
            'provider': llm_row.get('provider', ''),
            'source': 'db',
        }

    timeout = get_config_value('LLM_TIMEOUT', default=60, value_type='int')
    return {
        'model_name': model_name,
        'base_url': '',
        'timeout': int(timeout),
        'api_key': api_key,
        'provider': '',
        'source': 'missing',
    }


def invalidate_config_cache(key: Optional[str] = None) -> None:
    """失效 SystemConfig 缓存

    Args:
        key: 指定 key 仅失效该项；None 表示清空所有 SystemConfig 缓存
    """
    _invalidate_cache(_CFG_KEY_PREFIX, key)


def invalidate_llm_model_cache(model_name: Optional[str] = None,
                                model_type: Optional[str] = None) -> None:
    """失效 LLMModel 缓存

    Args:
        model_name: 指定模型名仅失效该项；None 表示清空所有 LLMModel 缓存
                    （模型管理 CRUD 后建议传 None 全清，避免遗漏 model_type 维度）
        model_type: 限定类型，与 model_name 组合定位
    """
    if model_name is None:
        _invalidate_cache(_LLM_KEY_PREFIX, None)
        return
    cache_key = _LLM_KEY_PREFIX + (f'{model_type}:{model_name}' if model_type else model_name)
    _delayed_double_delete([cache_key])


def _invalidate_cache(prefix: str, key: Optional[str]) -> None:
    """统一失效入口：单 key 删一项，None 删整组（按前缀扫描）"""
    if key is not None:
        _delayed_double_delete([prefix + key])
        return
    try:
        keys = cache.keys(prefix + '*')
        if keys:
            _delayed_double_delete(list(keys))
    except Exception as e:
        logger.warning(f'[config_loader] 按前缀扫描失败，降级整体清空: {e}')
        try:
            cache.clear()
        except Exception as e2:
            logger.error(f'[config_loader] 整体清空缓存失败: {e2}')


def _delayed_double_delete(keys) -> None:
    """延迟双删：立即删一次 + 1s 后再删一次

    解决并发场景下两类问题：
    1) 写库事务未提交就清缓存 → 另一线程读到 DB 旧值并回填缓存 → 缓存与 DB 不一致
    2) 写库后立即读仍命中旧缓存 → 延迟删除覆盖读旧值的窗口
    """
    if not keys:
        return
    try:
        cache.delete_many(keys)
    except Exception as e:
        logger.warning(f'[config_loader] 首次删除缓存失败: {e}')

    def _delayed_delete():
        time.sleep(1.0)
        try:
            cache.delete_many(keys)
        except Exception as e:
            logger.warning(f'[config_loader] 延迟删除缓存失败: {e}')

    t = threading.Thread(target=_delayed_delete, daemon=True)
    t.start()


def _read_config_from_db(key: str):
    """从 SystemConfig 表读取 value + value_type

    Returns: (value_str, value_type) ；DB 异常或未找到返回 (None, 'string')
    """
    try:
        from .models import SystemConfig
        row = SystemConfig.objects.filter(key=key).only('value', 'value_type').first()
        if row is None:
            return None, 'string'
        return row.value, row.value_type
    except Exception as e:
        logger.warning(f'[config_loader] 读 SystemConfig 失败 key={key}: {e}')
        return None, 'string'


def _read_llm_model_from_db(model_name: str, model_type: Optional[str]) -> Optional[Dict[str, Any]]:
    """从 LLMModel 表读取一条启用模型配置"""
    try:
        from .models import LLMModel
        qs = LLMModel.objects.filter(model_name=model_name, is_active=True)
        if model_type:
            qs = qs.filter(model_type=model_type)
        row = qs.first()
        if row is None:
            return None
        return {
            'id': row.id,
            'name': row.name,
            'provider': row.provider,
            'model_type': row.model_type,
            'base_url': row.base_url or '',
            'model_name': row.model_name,
            'timeout': row.timeout,
            'is_active': row.is_active,
        }
    except Exception as e:
        logger.warning(f'[config_loader] 读 LLMModel 失败 model={model_name}: {e}')
        return None


def _read_api_key_from_env(system_key: str) -> str:
    """从 Django settings 中读取 API Key（API Key 属敏感凭证，不入库）

    - LLM_* 系列：读 settings.LLM_API_KEY
    - EMBEDDING_MODEL/RERANK_MODEL：读 settings.EMBEDDING_API_KEY
    """
    from django.conf import settings
    if system_key in ('EMBEDDING_MODEL', 'RERANK_MODEL'):
        return getattr(settings, 'EMBEDDING_API_KEY', '') or ''
    return getattr(settings, 'LLM_API_KEY', '') or ''


def _cast_value(raw: Any, value_type: Optional[str]) -> Any:
    """按 value_type 转换为 Python 类型

    - bool: 兼容 'true'/'false'/'1'/'0' 等字符串
    - int/float: 容错空字符串返回 None（由调用方 default 处理）
    - json: 解析为 dict/list
    - string/None: 原样返回
    """
    if raw is None or value_type is None or value_type == 'string':
        return raw
    if value_type == 'bool':
        if isinstance(raw, bool):
            return raw
        return str(raw).lower().strip() in ('1', 'true', 'yes', 'on')
    if value_type == 'int':
        try:
            return int(str(raw).strip())
        except (ValueError, TypeError):
            return None
    if value_type == 'float':
        try:
            return float(str(raw).strip())
        except (ValueError, TypeError):
            return None
    if value_type == 'json':
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    return raw
