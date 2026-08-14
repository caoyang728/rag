"""
apps.analytics.services.deepeval_service 单元测试 —— DeepEval 12 维 LLM-as-judge 评估

覆盖范围：
- get_deepeval_model：EVAL_MODEL 未配置抛 ValueError / 正常构建 DeepSeekModel
- _build_preset_metrics：6 个预置指标（faithfulness/answer_relevancy/context_relevancy/
  hallucination/toxicity/bias）的构造参数（threshold=0.5、include_reason、async_mode=False）
- _build_geval_metrics：6 个 G-Eval 自定义业务指标（completeness/conciseness/clarity/
  professionalism/helpfulness/actionability）
- build_production_metrics：维度白名单控制（空配置 / 全部 12 维 / 部分维度 / 非法维度过滤）
- 维度全集：AnalyticsConfig._ALL_EVAL_DIMENSIONS 恰为 12 维
- evaluate_with_deepeval：单指标 happy path / 指标失败降级 0 分 / 反向语义维度反转
  （hallucination/toxicity/bias）/ 空 contexts 兜底 / score=None

说明：测试环境未安装 deepeval 包，deepeval_metrics 模块内部均为函数内局部导入
（from deepeval... import ...），因此通过 autouse fixture 向 sys.modules 注入
假的 deepeval 模块树，让被测试函数的导入语句可解析，metric 对象全部用 MagicMock。
"""
import sys
from types import ModuleType

import pytest
from unittest.mock import patch, MagicMock

from apps.analytics.services import deepeval_service
from rag_project.config import AnalyticsConfig

# 12 维全集（与 AnalyticsConfig._ALL_EVAL_DIMENSIONS 保持一致，供测试断言）
ALL_DIMS = [
    'faithfulness', 'hallucination', 'answer_relevancy', 'context_relevancy',
    'toxicity', 'bias', 'completeness', 'conciseness', 'clarity',
    'professionalism', 'helpfulness', 'actionability',
]


@pytest.fixture(autouse=True)
def _fake_deepeval(monkeypatch):
    """注入假的 deepeval 模块树（真实 deepeval 未安装，避免测试依赖外部包）

    被测试函数内部通过 from deepeval.metrics import ... 等局部导入，
    只需保证这些模块在 sys.modules 中可解析即可；metric 类用 MagicMock，
    调用后返回的实例自动具备任意属性（score/reason 由测试自行配置）。
    """
    fake = {}
    for name in ('deepeval', 'deepeval.metrics', 'deepeval.models', 'deepeval.test_case'):
        fake[name] = ModuleType(name)

    # deepeval.metrics 中的指标类
    for cls_name in ('FaithfulnessMetric', 'AnswerRelevancyMetric',
                     'ContextualRelevancyMetric', 'HallucinationMetric',
                     'ToxicityMetric', 'BiasMetric', 'GEval'):
        setattr(fake['deepeval.metrics'], cls_name, MagicMock(name=cls_name))
    # deepeval.models.DeepSeekModel
    fake['deepeval.models'].DeepSeekModel = MagicMock(name='DeepSeekModel')
    # deepeval.test_case.LLMTestCase + SingleTurnParams（G-Eval 需要 INPUT/ACTUAL_OUTPUT 枚举）
    fake['deepeval.test_case'].LLMTestCase = MagicMock(name='LLMTestCase')
    params = MagicMock(name='SingleTurnParams')
    params.INPUT = 'input'
    params.ACTUAL_OUTPUT = 'actual_output'
    fake['deepeval.test_case'].SingleTurnParams = params

    for name, mod in fake.items():
        monkeypatch.setitem(sys.modules, name, mod)


def _fake_metric(score, reason='', input_tokens=0, output_tokens=0):
    """构造一个带 score/reason 的可 measure 假指标

    原生模型(DeepEvalBaseLLM 子类)measure 后会在 metric 上回填实际
    input/output tokens,测试显式指定以验证 tokens_used 聚合逻辑。
    """
    m = MagicMock(name='metric')
    m.score = score
    m.reason = reason
    m.input_tokens = input_tokens
    m.output_tokens = output_tokens
    return m


# ============================================================================
# get_deepeval_model —— 构建 DeepEval 评估用 DeepSeek 模型
# ============================================================================
class TestGetDeepEvalModel:
    """get_deepeval_model 模型构建测试"""

    @pytest.mark.unit
    def test_missing_model_raises(self, settings):
        """EVAL_MODEL 未配置时抛 ValueError，防止用空模型名启动评估"""
        settings.LLM_API_KEY = 'sk-test'
        with patch('apps.system.config_loader.get_config_value', return_value=''):
            with pytest.raises(ValueError, match='EVAL_MODEL'):
                deepeval_service.get_deepeval_model()

    @pytest.mark.unit
    def test_model_constructed_with_config(self, settings):
        """配置存在时按 SystemConfig.EVAL_MODEL 构建 DeepSeekModel（temperature=0 保证可复现）"""
        settings.LLM_API_KEY = 'sk-test'
        fake_model = MagicMock(name='deepseek_instance')
        deepeval_service.get_deepeval_model  # noqa
        with patch('apps.system.config_loader.get_config_value',
                   return_value='deepseek-chat'), \
             patch('deepeval.models.DeepSeekModel', return_value=fake_model) as mock_cls:
            result = deepeval_service.get_deepeval_model()
            assert result is fake_model
            mock_cls.assert_called_once_with(
                model='deepseek-chat', api_key='sk-test', temperature=0)

    @pytest.mark.unit
    def test_explicit_model_overrides_config(self, settings):
        """显式传入 model 时优先使用，不再读取 SystemConfig.EVAL_MODEL"""
        settings.LLM_API_KEY = 'sk-test'
        fake_model = MagicMock(name='deepseek_instance')
        with patch('apps.system.config_loader.get_config_value') as mock_cfg, \
             patch('deepeval.models.DeepSeekModel', return_value=fake_model) as mock_cls:
            deepeval_service.get_deepeval_model(model='custom-model')
            mock_cfg.assert_not_called()
            mock_cls.assert_called_once_with(
                model='custom-model', api_key='sk-test', temperature=0)


# ============================================================================
# _build_preset_metrics / _build_geval_metrics —— 指标构造
# ============================================================================
class TestBuildMetrics:
    """指标构造参数测试"""

    @pytest.mark.unit
    def test_preset_metrics_six_dims(self):
        """预置指标恰为 6 个，且每个都带 async_mode=False（避免 Celery 同步任务 event loop 问题）"""
        metrics = deepeval_service._build_preset_metrics(MagicMock())
        assert len(metrics) == 6
        names = [n for n, _ in metrics]
        assert names == ['faithfulness', 'answer_relevancy', 'context_relevancy',
                         'hallucination', 'toxicity', 'bias']

    @pytest.mark.unit
    def test_preset_metrics_kwargs(self):
        """预置指标统一 threshold=0.5 + include_reason + async_mode=False"""
        model = MagicMock()
        deepeval_service._build_preset_metrics(model)
        from deepeval.metrics import FaithfulnessMetric, ToxicityMetric
        for cls in (FaithfulnessMetric, ToxicityMetric):
            _, kwargs = cls.call_args
            assert kwargs['model'] is model
            assert kwargs['threshold'] == 0.5
            assert kwargs['include_reason'] is True
            assert kwargs['async_mode'] is False

    @pytest.mark.unit
    def test_geval_metrics_six_dims(self):
        """G-Eval 自定义指标恰为 6 个（业务体验 + 答案质量主观维度）"""
        metrics = deepeval_service._build_geval_metrics(MagicMock())
        assert len(metrics) == 6
        names = [n for n, _ in metrics]
        assert names == ['completeness', 'conciseness', 'clarity',
                         'professionalism', 'helpfulness', 'actionability']

    @pytest.mark.unit
    def test_geval_metrics_params(self):
        """G-Eval 使用 INPUT + ACTUAL_OUTPUT 评估参数，且 async_mode=False"""
        deepeval_service._build_geval_metrics(MagicMock())
        from deepeval.metrics import GEval
        assert GEval.call_count == 6
        for call in GEval.call_args_list:
            _, kwargs = call
            assert kwargs['async_mode'] is False
            # evaluation_params 引用 SingleTurnParams 枚举
            assert kwargs['evaluation_params'] == ['input', 'actual_output']


# ============================================================================
# build_production_metrics —— 维度白名单控制（评估=展示强绑定）
# ============================================================================
class TestBuildProductionMetrics:
    """维度白名单构建测试"""

    @pytest.mark.unit
    def test_empty_dims_returns_empty(self):
        """用户主动清空维度配置 → 返回空列表，评估任务跳过"""
        with patch('apps.analytics.services.deepeval_service._build_preset_metrics') as m1, \
             patch('apps.analytics.services.deepeval_service._build_geval_metrics') as m2, \
             patch.object(AnalyticsConfig, 'eval_display_dimensions', return_value=[]):
            assert deepeval_service.build_production_metrics(MagicMock()) == []
            m1.assert_not_called()
            m2.assert_not_called()

    @pytest.mark.unit
    def test_all_12_dims_built(self):
        """全量配置 → 返回 12 个维度，顺序与 _ALL_EVAL_DIMENSIONS 一致"""
        model = MagicMock()
        preset = [(d, _fake_metric(1.0)) for d in ALL_DIMS[:6]]
        geval = [(d, _fake_metric(1.0)) for d in ALL_DIMS[6:]]
        with patch('apps.analytics.services.deepeval_service._build_preset_metrics', return_value=preset), \
             patch('apps.analytics.services.deepeval_service._build_geval_metrics', return_value=geval), \
             patch.object(AnalyticsConfig, 'eval_display_dimensions', return_value=list(ALL_DIMS)):
            result = deepeval_service.build_production_metrics(model)
        assert [n for n, _ in result] == ALL_DIMS
        assert len(result) == 12

    @pytest.mark.unit
    def test_partial_dims_filtered(self):
        """部分勾选 → 仅构建勾选维度，且保持配置中的勾选顺序"""
        preset = [(d, _fake_metric(1.0)) for d in ALL_DIMS[:6]]
        geval = [(d, _fake_metric(1.0)) for d in ALL_DIMS[6:]]
        with patch('apps.analytics.services.deepeval_service._build_preset_metrics', return_value=preset), \
             patch('apps.analytics.services.deepeval_service._build_geval_metrics', return_value=geval), \
             patch.object(AnalyticsConfig, 'eval_display_dimensions',
                          return_value=['toxicity', 'faithfulness']):
            result = deepeval_service.build_production_metrics(MagicMock())
        # 按 eval_display_dimensions 配置顺序输出（toxicity 在前）
        assert [n for n, _ in result] == ['toxicity', 'faithfulness']

    @pytest.mark.unit
    def test_unknown_dimension_dropped(self):
        """非法维度名被防御性过滤，不影响合法维度构建"""
        preset = [(d, _fake_metric(1.0)) for d in ALL_DIMS[:6]]
        geval = [(d, _fake_metric(1.0)) for d in ALL_DIMS[6:]]
        with patch('apps.analytics.services.deepeval_service._build_preset_metrics', return_value=preset), \
             patch('apps.analytics.services.deepeval_service._build_geval_metrics', return_value=geval), \
             patch.object(AnalyticsConfig, 'eval_display_dimensions',
                          return_value=['not-a-dim', 'bias']):
            result = deepeval_service.build_production_metrics(MagicMock())
        assert [n for n, _ in result] == ['bias']


# ============================================================================
# 维度全集 —— 恰为 12 维，四大类分布正确
# ============================================================================
class TestDimensionList:
    """12 维评估体系完整性测试"""

    @pytest.mark.unit
    def test_all_eval_dimensions_has_12(self):
        """维度全集恰为 12 个（检索1 + 答案6 + 安全2 + 业务3）"""
        assert len(AnalyticsConfig._ALL_EVAL_DIMENSIONS) == 12
        assert set(AnalyticsConfig._ALL_EVAL_DIMENSIONS) == set(ALL_DIMS)


# ============================================================================
# evaluate_with_deepeval —— 逐指标 measure 主流程
# ============================================================================
class TestEvaluateWithDeepEval:
    """evaluate_with_deepeval 评估流程测试"""

    def _call(self, metrics, contexts=None, **kwargs):
        """组装 mock 后调用 evaluate_with_deepeval"""
        with patch('apps.analytics.services.deepeval_service.get_deepeval_model',
                   return_value=MagicMock()), \
             patch('apps.analytics.services.deepeval_service.build_production_metrics',
                   return_value=metrics):
            return deepeval_service.evaluate_with_deepeval(
                question='问题', answer='回答', contexts=contexts or [], **kwargs)

    @pytest.mark.unit
    def test_happy_path_scores(self):
        """正常路径：分数按 4 位四舍五入，保留 dimension/reason/latency/tokens 结构"""
        metrics = [
            ('faithfulness', _fake_metric(0.8, '忠于上下文', input_tokens=130, output_tokens=70)),
            ('answer_relevancy', _fake_metric(0.654321, '基本切题')),
        ]
        results = self._call(metrics)
        assert len(results) == 2
        assert results[0]['dimension'] == 'faithfulness'
        assert results[0]['score'] == 0.8
        assert results[0]['reason'] == '忠于上下文'
        # 无 tokens 回填的指标(非原生模型/mock)兜底为 0;有回填时按 input+output 聚合
        assert results[0]['tokens_used'] == 200
        assert results[0]['input_tokens'] == 130
        assert results[0]['output_tokens'] == 70
        assert results[1]['tokens_used'] == 0
        # 0.654321 → 0.6543
        assert results[1]['score'] == 0.6543

    @pytest.mark.unit
    def test_inverted_dims_reversed(self):
        """反向语义维度反转：hallucination/toxicity/bias 原始 0=好 → 存 1.0"""
        metrics = [
            ('hallucination', _fake_metric(0.3, '无幻觉')),
            ('toxicity', _fake_metric(0.0, '无毒性')),
            ('bias', _fake_metric(0.2, '无偏见')),
            ('clarity', _fake_metric(0.9, '清晰')),
        ]
        results = {r['dimension']: r['score'] for r in self._call(metrics)}
        assert results['hallucination'] == pytest.approx(0.7)
        assert results['toxicity'] == 1.0
        assert results['bias'] == pytest.approx(0.8)
        # 非反向维度原样保留
        assert results['clarity'] == 0.9

    @pytest.mark.unit
    def test_metric_failure_degrades_to_zero(self):
        """单指标 measure 抛异常不中断整体，该维度记 0 分并记录失败原因"""
        bad = _fake_metric(0.8)
        bad.measure.side_effect = RuntimeError('llm down')
        results = self._call([('faithfulness', bad), ('clarity', _fake_metric(0.9))])
        assert len(results) == 2
        fail = results[0]
        assert fail['dimension'] == 'faithfulness'
        assert fail['score'] == 0.0
        assert fail['reason'].startswith('评估失败')
        assert results[1]['score'] == 0.9

    @pytest.mark.unit
    def test_score_none_returns_zero(self):
        """metric.score 为 None 时按 0 分处理，避免 float(None) 抛错"""
        results = self._call([('clarity', _fake_metric(None))])
        assert results[0]['score'] == 0.0

    @pytest.mark.unit
    def test_empty_contexts_use_empty_string(self):
        """contexts 为空时传 [''] 避免 DeepEval 抛 None 校验错误"""
        from deepeval.test_case import LLMTestCase
        with patch('apps.analytics.services.deepeval_service.get_deepeval_model',
                   return_value=MagicMock()), \
             patch('apps.analytics.services.deepeval_service.build_production_metrics',
                   return_value=[('clarity', _fake_metric(0.9))]):
            deepeval_service.evaluate_with_deepeval(
                question='q', answer='a', contexts=[])
        # LLMTestCase 以 [''] 作为 retrieval_context 兜底
        _, kwargs = LLMTestCase.call_args
        assert kwargs['retrieval_context'] == ['']
