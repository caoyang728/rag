"""
apps.analytics.management.commands.ragas_eval 测试 —— Ragas 评估命令的 CLI 编排

覆盖范围：
- 默认参数透传 run_full_pipeline，成功时输出测试集 ID / 样本数 / 指标均值 / 报告文件
- --skip-generate 未带 --testset → 报错并提前返回
- --skip-generate 指定不存在的文件 → 报错并提前返回
- --skip-generate 复用已有测试集 → load_testset + 传入 samples
- 流水线抛异常 → 降级输出错误并返回

handle 内部延迟导入 run_full_pipeline/load_testset，直接 patch 模块属性即可
（ragas_pipeline 模块级无外部依赖），无需真实 Ragas/LLM/DB。
"""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command


def _pipeline_result():
    """构造 run_full_pipeline 成功返回结构"""
    return {
        'testset_id': 'testset_20260802_120000_abc123',
        'samples': [{'question': 'q1'}, {'question': 'q2'}],
        'summary': {'faithfulness': 0.9, 'answer_relevancy': None},
        'report_paths': {'json': 'eval_reports/report.json', 'md': 'eval_reports/report.md'},
    }


@patch('apps.analytics.services.ragas_service.run_full_pipeline')
def test_handle_success_outputs_summary(mock_pipeline):
    """成功时输出测试集 ID、样本数、指标均值与报告路径"""
    mock_pipeline.return_value = _pipeline_result()
    out, err = StringIO(), StringIO()

    call_command('ragas_eval', stdout=out, stderr=err)

    mock_pipeline.assert_called_once()
    kwargs = mock_pipeline.call_args.kwargs
    assert kwargs['testset_size'] == 20
    assert kwargs['limit_docs'] == 50
    assert kwargs['root_type'] is None
    assert kwargs['model'] is None
    # 输出目录默认落在 scripts/tmp/eval_reports(临时产物规范,不散落到项目其他位置)
    assert kwargs['output_dir'] == 'scripts/tmp/eval_reports'
    assert kwargs['samples'] is None

    text = out.getvalue()
    assert 'testset_20260802_120000_abc123' in text
    assert '样本数: 2' in text
    assert 'faithfulness: 0.9' in text
    assert 'answer_relevancy: N/A' in text
    assert 'eval_reports/report.json' in text


@patch('apps.analytics.services.ragas_service.run_full_pipeline')
def test_handle_skip_generate_without_testset_reports_error(mock_pipeline):
    """--skip-generate 未带 --testset 时应报错并跳过流水线"""
    out, err = StringIO(), StringIO()

    call_command('ragas_eval', '--skip-generate', stdout=out, stderr=err)

    assert '需配合 --testset' in err.getvalue()
    mock_pipeline.assert_not_called()


@patch('apps.analytics.services.ragas_service.run_full_pipeline')
def test_handle_skip_generate_missing_file_reports_error(mock_pipeline, tmp_path):
    """--skip-generate 指定的测试集文件不存在时应报错"""
    out, err = StringIO(), StringIO()

    call_command('ragas_eval', '--skip-generate',
                 '--testset', str(tmp_path / 'none.json'),
                 stdout=out, stderr=err)

    assert '文件不存在' in err.getvalue()
    mock_pipeline.assert_not_called()


@patch('apps.analytics.services.ragas_service.load_testset')
@patch('apps.analytics.services.ragas_service.run_full_pipeline')
def test_handle_skip_generate_reuses_testset(mock_pipeline, mock_load, tmp_path):
    """--skip-generate 复用已有测试集时 load_testset 并传入 samples"""
    ts_file = tmp_path / 'testset.json'
    ts_file.write_text('[]', encoding='utf-8')
    mock_load.return_value = [{'question': '已有问题'}]
    mock_pipeline.return_value = _pipeline_result()
    out, err = StringIO(), StringIO()

    call_command('ragas_eval', '--skip-generate', '--testset', str(ts_file),
                 stdout=out, stderr=err)

    mock_load.assert_called_once_with(str(ts_file))
    assert mock_pipeline.call_args.kwargs['samples'] == [{'question': '已有问题'}]
    assert '复用测试集' in out.getvalue()


@patch('apps.analytics.services.ragas_service.run_full_pipeline')
def test_handle_pipeline_exception_reports_error(mock_pipeline):
    """流水线抛异常时应输出错误并返回"""
    mock_pipeline.side_effect = RuntimeError('LLM 超时')
    out, err = StringIO(), StringIO()

    with patch('apps.analytics.management.commands.ragas_eval.logger'):
        call_command('ragas_eval', stdout=out, stderr=err)

    assert '评估失败: LLM 超时' in err.getvalue()
    assert '评估完成' not in out.getvalue()
