"""
apps.llm.factory 单元测试 —— LLM 工厂（双模型 + 单例缓存）

覆盖范围：
- get_llm：显式 model / 读 SystemConfig / 未配置抛 ValueError / 单例缓存
- get_llm_advanced：已配置高级模型 / 未配置回退基础模型
- reset_llm：清空单例缓存
- _resolve_llm_params：LLMModel 命中 / 未命中（base_url 留空 + timeout 回退）

全部用 mock 而非 DB：
factory 自身只做参数拼装与单例管理，真正的配置读取委托给 config_loader（其缓存
逻辑另有独立测试）。这里若再连真实 DB/Redis，会掩盖“单例命中即不查库”等关键契约，
因此统一 mock config_loader.get_config_value / get_llm_model_config，并 patch
DeepSeekProvider 以避免真实实例化 OpenAI 客户端（构造即读 env，且无意义于本测试）。

settings.LLM_API_KEY 通过 pytest-django 的 settings fixture 覆写，因为
_resolve_llm_params 内部用 getattr(settings, 'LLM_API_KEY', '') 局部读取。
"""
import pytest
from unittest.mock import patch, MagicMock

from apps.llm import factory
from apps.llm.factory import (
    get_llm,
    get_llm_advanced,
    reset_llm,
    _resolve_llm_params,
    _instances,
)


# ============================================================================
# get_llm —— 主入口：读配置 / 单例 / 显式 model
# ============================================================================
class TestGetLLM:
    """get_llm 配置读取与单例缓存测试"""

    @pytest.mark.unit
    @patch('apps.llm.factory.DeepSeekProvider')
    @patch('apps.system.config_loader.get_llm_model_config')
    def test_get_llm_with_explicit_model(self, mock_llm_model, mock_provider_cls, settings):
        """显式传入 model 参数时直接用该 model 实例化，不再读 LLM_BASE_MODEL"""
        reset_llm()
        settings.LLM_API_KEY = 'sk-test'
        # LLMModel 命中：提供 base_url / timeout / provider
        mock_llm_model.return_value = {
            'base_url': 'https://api.deepseek.com',
            'timeout': 120,
            'provider': 'deepseek',
        }
        inst = get_llm('deepseek-chat')

        # 应只构造一次，且参数从 _resolve_llm_params 透传
        mock_provider_cls.assert_called_once()
        kwargs = mock_provider_cls.call_args.kwargs
        assert kwargs['model'] == 'deepseek-chat'
        assert kwargs['base_url'] == 'https://api.deepseek.com'
        assert kwargs['api_key'] == 'sk-test'
        assert kwargs['timeout'] == 120
        # 返回的就是构造出的实例
        assert inst is mock_provider_cls.return_value

    @pytest.mark.unit
    @patch('apps.llm.factory.DeepSeekProvider')
    @patch('apps.system.config_loader.get_config_value')
    @patch('apps.system.config_loader.get_llm_model_config')
    def test_get_llm_reads_config(self, mock_llm_model, mock_get_cfg, mock_provider_cls, settings):
        """未传 model 时应回退读取 SystemConfig.LLM_BASE_MODEL"""
        reset_llm()
        settings.LLM_API_KEY = 'sk-test'
        # get_config_value 模拟 LLM_BASE_MODEL 返回值
        mock_get_cfg.return_value = 'deepseek-chat'
        mock_llm_model.return_value = {'base_url': 'http://x', 'timeout': 60, 'provider': 'deepseek'}

        get_llm()

        # 应以 'LLM_BASE_MODEL' 作为 key 读取配置
        mock_get_cfg.assert_any_call('LLM_BASE_MODEL', default='', value_type='string')
        # 实例化时 model 即为配置读到的值
        assert mock_provider_cls.call_args.kwargs['model'] == 'deepseek-chat'

    @pytest.mark.unit
    @patch('apps.system.config_loader.get_config_value')
    def test_get_llm_no_model_configured(self, mock_get_cfg, settings):
        """LLM_BASE_MODEL 为空时应抛 ValueError，由调用方决定降级策略"""
        reset_llm()
        settings.LLM_API_KEY = ''
        mock_get_cfg.return_value = ''  # 未配置基础模型

        with pytest.raises(ValueError):
            get_llm()

    @pytest.mark.unit
    @patch('apps.llm.factory.DeepSeekProvider')
    @patch('apps.system.config_loader.get_llm_model_config')
    def test_get_llm_singleton(self, mock_llm_model, mock_provider_cls, settings):
        """同一 model 第二次获取应命中单例缓存，不再构造新实例"""
        reset_llm()
        settings.LLM_API_KEY = 'sk-test'
        mock_llm_model.return_value = {'base_url': 'http://x', 'timeout': 60, 'provider': 'deepseek'}

        inst1 = get_llm('deepseek-chat')
        inst2 = get_llm('deepseek-chat')

        # 同一对象且 Provider 仅构造一次（证明走的是缓存而非重建）
        assert inst1 is inst2
        assert mock_provider_cls.call_count == 1


# ============================================================================
# get_llm_advanced —— 高级模型入口：已配置 / 回退基础模型
# ============================================================================
class TestGetLLMAdvanced:
    """get_llm_advanced 高级模型回退策略测试"""

    @pytest.mark.unit
    @patch('apps.llm.factory.DeepSeekProvider')
    @patch('apps.system.config_loader.get_config_value')
    @patch('apps.system.config_loader.get_llm_model_config')
    def test_get_llm_advanced_configured(self, mock_llm_model, mock_get_cfg, mock_provider_cls, settings):
        """LLM_ADVANCED_MODEL 已配置时应使用高级模型实例化"""
        reset_llm()
        settings.LLM_API_KEY = 'sk-test'
        # get_config_value 在 get_llm_advanced 中读 LLM_ADVANCED_MODEL
        mock_get_cfg.return_value = 'deepseek-v4-pro'
        mock_llm_model.return_value = {'base_url': 'http://x', 'timeout': 60, 'provider': 'deepseek'}

        get_llm_advanced()

        mock_get_cfg.assert_any_call('LLM_ADVANCED_MODEL', default='', value_type='string')
        assert mock_provider_cls.call_args.kwargs['model'] == 'deepseek-v4-pro'

    @pytest.mark.unit
    @patch('apps.llm.factory.get_llm')
    @patch('apps.system.config_loader.get_config_value')
    def test_get_llm_advanced_fallback(self, mock_get_cfg, mock_get_llm, settings):
        """LLM_ADVANCED_MODEL 为空时应回退到基础模型 get_llm()"""
        reset_llm()
        mock_get_cfg.return_value = ''  # 高级模型未配置
        mock_get_llm.return_value = MagicMock(name='base_llm')

        result = get_llm_advanced()

        # 应回退调用 get_llm() 并返回其实例
        mock_get_llm.assert_called_once()
        assert result is mock_get_llm.return_value


# ============================================================================
# reset_llm —— 单例缓存重置（测试辅助）
# ============================================================================
class TestResetLLM:
    """reset_llm 缓存清空测试"""

    @pytest.mark.unit
    def test_reset_llm_clears_instances(self):
        """reset_llm 应清空 _instances 字典"""
        # 预先塞入两个伪实例模拟已初始化状态
        _instances['model-a'] = MagicMock(name='a')
        _instances['model-b'] = MagicMock(name='b')
        assert len(_instances) == 2

        reset_llm()

        assert _instances == {}


# ============================================================================
# _resolve_llm_params —— 参数拼装：LLMModel 命中 / 未命中
# ============================================================================
class TestResolveLLMParams:
    """_resolve_llm_params 从 LLMModel 表拼装参数测试"""

    @pytest.mark.unit
    @patch('apps.system.config_loader.get_config_value')
    @patch('apps.system.config_loader.get_llm_model_config')
    def test_resolve_llm_params_with_db(self, mock_llm_model, mock_get_cfg, settings):
        """LLMModel 命中时返回 (api_key, base_url, timeout, provider) 全量参数"""
        settings.LLM_API_KEY = 'sk-test'
        mock_llm_model.return_value = {
            'base_url': 'https://api.deepseek.com',
            'timeout': 90,
            'provider': 'deepseek',
        }

        api_key, base_url, timeout, provider = _resolve_llm_params('deepseek-chat')

        assert api_key == 'sk-test'
        assert base_url == 'https://api.deepseek.com'
        assert timeout == 90
        assert provider == 'deepseek'
        # 命中行时不应再回退查 LLM_TIMEOUT
        mock_get_cfg.assert_not_called()

    @pytest.mark.unit
    @patch('apps.system.config_loader.get_config_value')
    @patch('apps.system.config_loader.get_llm_model_config')
    def test_resolve_llm_params_no_db(self, mock_llm_model, mock_get_cfg, settings):
        """LLMModel 未命中时 base_url 留空、timeout 回退 SystemConfig.LLM_TIMEOUT"""
        settings.LLM_API_KEY = 'sk-test'
        mock_llm_model.return_value = None
        # LLM_TIMEOUT 回退值
        mock_get_cfg.return_value = 60

        api_key, base_url, timeout, provider = _resolve_llm_params('deepseek-chat')

        assert api_key == 'sk-test'
        assert base_url == ''  # 未命中，base_url 留空
        assert timeout == 60   # 回退到 LLM_TIMEOUT
        assert provider == ''  # 未命中无 provider
        mock_get_cfg.assert_called_once_with('LLM_TIMEOUT', default=60, value_type='int')

    @pytest.mark.unit
    @patch('apps.system.config_loader.get_config_value')
    @patch('apps.system.config_loader.get_llm_model_config')
    def test_resolve_llm_params_db_row_missing_timeout(self, mock_llm_model, mock_get_cfg, settings):
        """LLMModel 命中但 timeout 缺失时，回退 SystemConfig.LLM_TIMEOUT 兜底"""
        settings.LLM_API_KEY = 'sk-test'
        mock_llm_model.return_value = {
            'base_url': 'https://api.deepseek.com',
            'timeout': None,  # 行内 timeout 缺失
            'provider': 'deepseek',
        }
        mock_get_cfg.return_value = 75  # LLM_TIMEOUT 兜底

        _, _, timeout, _ = _resolve_llm_params('deepseek-chat')

        assert timeout == 75
