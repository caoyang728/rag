"""
apps.system.config_loader 单元测试 —— 系统配置统一读取入口（Redis → DB 两层缓存）

覆盖范围：
- _cast_value：bool/int/float/json/string/None 六类类型转换与容错
- get_config_value：缓存命中 / 缓存未命中 DB 命中 / DB 未命中 / Redis 异常降级
- get_llm_model_config：缓存命中 / 缓存未命中 DB 命中 / 未找到
- get_llm_config_by_system_key：完整拼装 / 缺失 model_name / LLMModel 未找到
- invalidate_config_cache / invalidate_llm_model_cache：单 key 与全量扫描清空
- _delayed_double_delete：立即删 + 启动延迟删线程
- _read_api_key_from_env：LLM 与 Embedding 两类 API Key 来源

config_loader 是 LLM/检索等业务侧的配置读取热点，缓存读写与降级逻辑需独立验证，
不耦合真实 Redis 与 DB，避免环境依赖掩盖缓存击穿/脏读问题。
"""
import json

import pytest
from unittest.mock import patch, MagicMock

from apps.system import config_loader


# ============================================================================
# _cast_value —— 按类型转换字符串为 Python 类型，转换失败需安全返回 None
# ============================================================================
class TestCastValue:
    """_cast_value 类型转换测试"""

    @pytest.mark.unit
    def test_cast_value_bool(self):
        """bool 类型：兼容 'true'/'1'/'false'/'0' 等字符串与原生 bool"""
        assert config_loader._cast_value('true', 'bool') is True
        assert config_loader._cast_value('1', 'bool') is True
        assert config_loader._cast_value('false', 'bool') is False
        assert config_loader._cast_value('0', 'bool') is False
        # 原生 bool 直接返回，不再走字符串归一化
        assert config_loader._cast_value(True, 'bool') is True

    @pytest.mark.unit
    def test_cast_value_int(self):
        """int 类型：可解析返回整数，否则返回 None 由调用方 default 兜底"""
        assert config_loader._cast_value('42', 'int') == 42
        assert config_loader._cast_value('abc', 'int') is None
        # 空字符串无法转 int，返回 None
        assert config_loader._cast_value('', 'int') is None

    @pytest.mark.unit
    def test_cast_value_float(self):
        """float 类型：可解析返回浮点，否则返回 None"""
        assert config_loader._cast_value('3.14', 'float') == 3.14
        assert config_loader._cast_value('abc', 'float') is None

    @pytest.mark.unit
    def test_cast_value_json(self):
        """json 类型：已是 dict/list 原样返回，字符串尝试解析，失败返回 None"""
        assert config_loader._cast_value('{"a":1}', 'json') == {"a": 1}
        assert config_loader._cast_value('invalid', 'json') is None
        # 已经是 dict 时直接返回，避免二次序列化
        assert config_loader._cast_value({"a": 1}, 'json') == {"a": 1}

    @pytest.mark.unit
    def test_cast_value_string(self):
        """string 类型：原样返回；None 输入也原样返回"""
        assert config_loader._cast_value('hello', 'string') == 'hello'
        assert config_loader._cast_value(None, 'string') is None

    @pytest.mark.unit
    def test_cast_value_none_type(self):
        """value_type=None 表示不转换，任意值原样返回"""
        assert config_loader._cast_value('raw_text', None) == 'raw_text'
        assert config_loader._cast_value(123, None) == 123
        assert config_loader._cast_value(None, None) is None


# ============================================================================
# get_config_value —— Redis → DB 两层缓存读取主流程
# ============================================================================
class TestGetConfigValue:
    """get_config_value 缓存与降级流程测试"""

    @pytest.mark.unit
    def test_get_config_value_cache_hit(self):
        """缓存命中时直接返回并按 value_type 转换，不触发 DB 查询"""
        with patch.object(config_loader, 'cache') as mock_cache, \
             patch.object(config_loader, '_read_config_from_db') as mock_db:
            mock_cache.get.return_value = '42'
            # 缓存命中走 int 转换
            result = config_loader.get_config_value('LLM_TIMEOUT', default=60, value_type='int')
            assert result == 42
            # 缓存命中不应再查 DB
            mock_db.assert_not_called()

    @pytest.mark.unit
    def test_get_config_value_cache_miss_db_hit(self):
        """缓存未命中时回源 DB，命中后回填 Redis 并按类型返回"""
        with patch.object(config_loader, 'cache') as mock_cache, \
             patch.object(config_loader, '_read_config_from_db') as mock_db:
            mock_cache.get.return_value = None
            # DB 返回 (raw_value, value_type)
            mock_db.return_value = ('42', 'int')
            result = config_loader.get_config_value('LLM_TIMEOUT', default=60, value_type='int')
            assert result == 42
            # 命中 DB 后应回填缓存
            mock_cache.set.assert_called_once()
            args = mock_cache.set.call_args[0]
            assert args[0] == 'sys:cfg:LLM_TIMEOUT'
            assert args[1] == '42'
            assert args[2] == config_loader._CACHE_TTL

    @pytest.mark.unit
    def test_get_config_value_cache_miss_db_miss(self):
        """缓存与 DB 均未命中时返回 default"""
        with patch.object(config_loader, 'cache') as mock_cache, \
             patch.object(config_loader, '_read_config_from_db') as mock_db:
            mock_cache.get.return_value = None
            # DB 返回 (None, 'string') 表示未找到
            mock_db.return_value = (None, 'string')
            result = config_loader.get_config_value('MISSING_KEY', default='fallback')
            assert result == 'fallback'
            # 未命中不应回填缓存
            mock_cache.set.assert_not_called()

    @pytest.mark.unit
    def test_get_config_value_redis_error(self):
        """Redis 异常时降级到 DB，保证缓存不可用不影响业务读取"""
        with patch.object(config_loader, 'cache') as mock_cache, \
             patch.object(config_loader, '_read_config_from_db') as mock_db:
            # cache.get 抛异常应被捕获并继续走 DB
            mock_cache.get.side_effect = RuntimeError('redis down')
            mock_db.return_value = ('42', 'int')
            result = config_loader.get_config_value('LLM_TIMEOUT', default=60, value_type='int')
            assert result == 42
            # 异常后仍应回填缓存
            mock_cache.set.assert_called_once()

    @pytest.mark.unit
    def test_get_config_value_empty_string_db_returns_default(self):
        """DB 中 value 为空字符串时按未命中处理，返回 default"""
        with patch.object(config_loader, 'cache') as mock_cache, \
             patch.object(config_loader, '_read_config_from_db') as mock_db:
            mock_cache.get.return_value = None
            # 空字符串视同未命中（避免回填空值并返回 None）
            mock_db.return_value = ('', 'string')
            result = config_loader.get_config_value('EMPTY_KEY', default='def')
            assert result == 'def'


# ============================================================================
# get_llm_model_config —— LLMModel 配置查询（带缓存）
# ============================================================================
class TestGetLLMModelConfig:
    """get_llm_model_config 缓存与回源测试"""

    @pytest.mark.unit
    def test_get_llm_model_config_cache_hit(self):
        """缓存命中直接返回 dict，不查 DB"""
        with patch.object(config_loader, 'cache') as mock_cache, \
             patch.object(config_loader, '_read_llm_model_from_db') as mock_db:
            cached_data = {'id': 1, 'model_name': 'deepseek-chat', 'base_url': 'http://x'}
            mock_cache.get.return_value = cached_data
            result = config_loader.get_llm_model_config('deepseek-chat')
            assert result == cached_data
            mock_db.assert_not_called()

    @pytest.mark.unit
    def test_get_llm_model_config_cache_miss_db_hit(self):
        """缓存未命中回源 DB，命中后回填缓存"""
        with patch.object(config_loader, 'cache') as mock_cache, \
             patch.object(config_loader, '_read_llm_model_from_db') as mock_db:
            mock_cache.get.return_value = None
            db_data = {'id': 1, 'model_name': 'deepseek-chat', 'base_url': 'http://x'}
            mock_db.return_value = db_data
            result = config_loader.get_llm_model_config('deepseek-chat', model_type='llm')
            assert result == db_data
            # 验证 cache_key 包含 model_type 前缀，避免跨类型同名误匹配
            args = mock_cache.set.call_args[0]
            assert args[0] == 'sys:llm:llm:deepseek-chat'

    @pytest.mark.unit
    def test_get_llm_model_config_not_found(self):
        """DB 也未找到时返回 None，且不回填缓存"""
        with patch.object(config_loader, 'cache') as mock_cache, \
             patch.object(config_loader, '_read_llm_model_from_db') as mock_db:
            mock_cache.get.return_value = None
            mock_db.return_value = None
            result = config_loader.get_llm_model_config('not-exist')
            assert result is None
            # 未找到不应回填缓存，避免缓存 None 污染
            mock_cache.set.assert_not_called()


# ============================================================================
# get_llm_config_by_system_key —— 便捷拼装完整 LLM 调用配置
# ============================================================================
class TestGetLLMConfigBySystemKey:
    """get_llm_config_by_system_key 拼装测试"""

    @pytest.mark.unit
    def test_get_llm_config_by_system_key_found(self):
        """model_name 存在 + LLMModel 命中：返回 source='db' 的完整配置"""
        with patch.object(config_loader, 'get_config_value') as mock_get_cfg, \
             patch.object(config_loader, 'get_llm_model_config') as mock_get_llm, \
             patch.object(config_loader, '_read_api_key_from_env') as mock_api_key:
            # SystemConfig 返回模型名
            mock_get_cfg.return_value = 'deepseek-chat'
            # LLMModel 返回带 base_url/timeout 的行
            mock_get_llm.return_value = {
                'base_url': 'https://api.deepseek.com',
                'timeout': 120,
                'provider': 'deepseek',
            }
            mock_api_key.return_value = 'sk-xxx'
            result = config_loader.get_llm_config_by_system_key('LLM_BASE_MODEL')
            assert result['model_name'] == 'deepseek-chat'
            assert result['base_url'] == 'https://api.deepseek.com'
            assert result['timeout'] == 120
            assert result['api_key'] == 'sk-xxx'
            assert result['provider'] == 'deepseek'
            assert result['source'] == 'db'

    @pytest.mark.unit
    def test_get_llm_config_by_system_key_missing_model(self):
        """SystemConfig 中 model_name 为空：返回 source='missing' 的兜底配置"""
        with patch.object(config_loader, 'get_config_value') as mock_get_cfg, \
             patch.object(config_loader, 'get_llm_model_config') as mock_get_llm:
            # 空模型名应直接返回 missing 配置，不再查 LLMModel
            mock_get_cfg.return_value = ''
            result = config_loader.get_llm_config_by_system_key('LLM_BASE_MODEL')
            assert result['source'] == 'missing'
            assert result['model_name'] == ''
            assert result['base_url'] == ''
            assert result['timeout'] == 60
            mock_get_llm.assert_not_called()

    @pytest.mark.unit
    def test_get_llm_config_by_system_key_no_llm_row(self):
        """model_name 存在但 LLMModel 未找到：source='missing'，base_url 为空"""
        with patch.object(config_loader, 'get_config_value') as mock_get_cfg, \
             patch.object(config_loader, 'get_llm_model_config') as mock_get_llm, \
             patch.object(config_loader, '_read_api_key_from_env') as mock_api_key:
            mock_get_cfg.return_value = 'deepseek-chat'
            # LLMModel 未找到（已被停用或不存在）
            mock_get_llm.return_value = None
            # LLM_TIMEOUT 兜底
            mock_get_cfg.side_effect = ['deepseek-chat', 90]
            mock_api_key.return_value = 'sk-yyy'
            result = config_loader.get_llm_config_by_system_key('LLM_BASE_MODEL')
            assert result['source'] == 'missing'
            assert result['model_name'] == 'deepseek-chat'
            assert result['base_url'] == ''
            # timeout 来自 LLM_TIMEOUT 配置
            assert result['timeout'] == 90
            assert result['api_key'] == 'sk-yyy'


# ============================================================================
# invalidate_config_cache / invalidate_llm_model_cache —— 缓存失效（延迟双删）
# ============================================================================
class TestInvalidateCache:
    """缓存失效函数测试"""

    @pytest.mark.unit
    def test_invalidate_config_cache_single_key(self):
        """指定 key 时仅失效该项（立即删一次）"""
        with patch.object(config_loader, '_delayed_double_delete') as mock_ddd:
            config_loader.invalidate_config_cache('LLM_TIMEOUT')
            mock_ddd.assert_called_once_with(['sys:cfg:LLM_TIMEOUT'])

    @pytest.mark.unit
    def test_invalidate_config_cache_all_keys(self):
        """key=None 时按前缀扫描清空全部 SystemConfig 缓存"""
        with patch.object(config_loader, 'cache') as mock_cache, \
             patch.object(config_loader, '_delayed_double_delete') as mock_ddd:
            mock_cache.keys.return_value = ['sys:cfg:LLM_TIMEOUT', 'sys:cfg:LLM_API_KEY']
            config_loader.invalidate_config_cache(None)
            mock_cache.keys.assert_called_once_with('sys:cfg:*')
            # 扫描到的所有 key 走延迟双删
            mock_ddd.assert_called_once_with(['sys:cfg:LLM_TIMEOUT', 'sys:cfg:LLM_API_KEY'])

    @pytest.mark.unit
    def test_invalidate_llm_model_cache_single(self):
        """指定 model_name 时按前缀 + model_name 精准失效"""
        with patch.object(config_loader, '_delayed_double_delete') as mock_ddd:
            config_loader.invalidate_llm_model_cache('deepseek-chat')
            mock_ddd.assert_called_once_with(['sys:llm:deepseek-chat'])

    @pytest.mark.unit
    def test_invalidate_llm_model_cache_single_with_type(self):
        """带 model_type 时 cache_key 应包含类型前缀，避免跨类型同名误删"""
        with patch.object(config_loader, '_delayed_double_delete') as mock_ddd:
            config_loader.invalidate_llm_model_cache('text-embedding', model_type='embedding')
            mock_ddd.assert_called_once_with(['sys:llm:embedding:text-embedding'])

    @pytest.mark.unit
    def test_invalidate_llm_model_cache_all(self):
        """model_name=None 时按前缀清空全部 LLMModel 缓存"""
        with patch.object(config_loader, 'cache') as mock_cache, \
             patch.object(config_loader, '_delayed_double_delete') as mock_ddd:
            mock_cache.keys.return_value = ['sys:llm:llm:deepseek-chat']
            config_loader.invalidate_llm_model_cache(None)
            mock_cache.keys.assert_called_once_with('sys:llm:*')
            mock_ddd.assert_called_once_with(['sys:llm:llm:deepseek-chat'])

    @pytest.mark.unit
    def test_invalidate_cache_scan_fallback_to_clear(self):
        """keys 扫描异常时降级整体 cache.clear，保证缓存最终被清掉"""
        with patch.object(config_loader, 'cache') as mock_cache:
            mock_cache.keys.side_effect = RuntimeError('scan not supported')
            config_loader.invalidate_config_cache(None)
            mock_cache.clear.assert_called_once()


# ============================================================================
# _delayed_double_delete —— 立即删 + 启动延迟删线程
# ============================================================================
class TestDelayedDoubleDelete:
    """_delayed_double_delete 延迟双删实现测试"""

    @pytest.mark.unit
    def test_delayed_double_delete_empty_keys_noop(self):
        """空 key 列表直接返回，不触发任何缓存操作"""
        with patch.object(config_loader, 'cache') as mock_cache:
            config_loader._delayed_double_delete([])
            mock_cache.delete_many.assert_not_called()

    @pytest.mark.unit
    def test_delayed_double_delete_calls_delete_many_and_starts_thread(self):
        """非空 key 列表：立即 delete_many 一次并启动延迟删线程"""
        with patch.object(config_loader, 'cache') as mock_cache, \
             patch.object(config_loader.threading, 'Thread') as mock_thread:
            config_loader._delayed_double_delete(['sys:cfg:a', 'sys:cfg:b'])
            mock_cache.delete_many.assert_called_once_with(['sys:cfg:a', 'sys:cfg:b'])
            # 延迟删线程应被启动（daemon=True，避免阻塞测试退出）
            mock_thread.assert_called_once()
            assert mock_thread.call_args[1].get('daemon') is True
            mock_thread.return_value.start.assert_called_once()

    @pytest.mark.unit
    def test_delayed_double_delete_first_delete_error_swallowed(self):
        """首次 delete_many 异常被吞掉，线程仍启动（延迟删兜底）"""
        with patch.object(config_loader, 'cache') as mock_cache, \
             patch.object(config_loader.threading, 'Thread') as mock_thread:
            mock_cache.delete_many.side_effect = RuntimeError('redis down')
            # 不应抛异常
            config_loader._delayed_double_delete(['sys:cfg:a'])
            mock_thread.assert_called_once()


# ============================================================================
# _read_api_key_from_env —— 从 settings 读取 API Key（LLM / Embedding）
# 注意：函数内部用 `from django.conf import settings` 局部导入，
# 因此用 pytest-django 的 settings fixture 覆写属性，而非 patch 模块符号。
# ============================================================================
class TestReadApiKeyFromEnv:
    """_read_api_key_from_env API Key 来源测试"""

    @pytest.mark.unit
    def test_read_api_key_from_env_llm(self, settings):
        """LLM_* 系列 key 读 settings.LLM_API_KEY"""
        settings.LLM_API_KEY = 'sk-llm-secret'
        settings.EMBEDDING_API_KEY = 'sk-emb-secret'
        result = config_loader._read_api_key_from_env('LLM_BASE_MODEL')
        assert result == 'sk-llm-secret'

    @pytest.mark.unit
    def test_read_api_key_from_env_embedding(self, settings):
        """EMBEDDING_MODEL / RERANK_MODEL 读 settings.EMBEDDING_API_KEY"""
        settings.LLM_API_KEY = 'sk-llm-secret'
        settings.EMBEDDING_API_KEY = 'sk-emb-secret'
        # embedding 与 rerank 共用 EMBEDDING_API_KEY
        assert config_loader._read_api_key_from_env('EMBEDDING_MODEL') == 'sk-emb-secret'
        assert config_loader._read_api_key_from_env('RERANK_MODEL') == 'sk-emb-secret'

    @pytest.mark.unit
    def test_read_api_key_from_env_missing_returns_empty(self, settings):
        """settings 未配置对应 key 时返回空串，getattr 回退默认值避免抛 AttributeError"""
        settings.LLM_API_KEY = ''
        result = config_loader._read_api_key_from_env('LLM_BASE_MODEL')
        assert result == ''
