"""
apps.graph.tasks 测试 —— 图谱 Celery 任务

覆盖范围：
- graph_extract_task：先清理旧图谱数据再批量抽取
- community_detection_task：获取 LLM 并运行社区检测
"""
import pytest
from unittest.mock import patch, MagicMock


@pytest.mark.unit
@patch('apps.graph.extractor.batch_extract_for_document')
@patch('apps.graph.sync._clean_graph_data')
def test_graph_extract_task_cleans_then_extracts(mock_clean, mock_batch):
    """graph_extract_task 应先清理旧图谱数据再批量抽取（增量一致性）"""
    from apps.graph.tasks import graph_extract_task
    mock_clean.return_value = {
        'relations': 2, 'entities_deleted': 1,
        'entities_kept': 1, 'entities_to_refresh': 0,
    }
    graph_extract_task(123)
    mock_clean.assert_called_once_with(123)
    mock_batch.assert_called_once_with(123)


@pytest.mark.unit
@patch('apps.graph.community.run_community_detection')
@patch('apps.llm.factory.get_llm')
def test_community_detection_task(mock_get_llm, mock_run):
    """community_detection_task 应获取 LLM 并运行社区检测，返回社区数量"""
    from apps.graph.tasks import community_detection_task
    mock_run.return_value = 5
    count = community_detection_task()
    assert count == 5
    mock_get_llm.assert_called_once()
    mock_run.assert_called_once()
