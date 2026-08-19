"""
apps.graph.management.commands.recover_graph 测试

覆盖范围：
- Command.handle：管理命令入口，委托 _recover_stuck_graph_docs 执行恢复
  - 默认（不带 --force）：与自愈任务行为一致，跳过活跃标记
  - --force 参数：跳过活跃标记检查，强制恢复
  - 无卡死文档时输出提示信息
"""
import pytest
from unittest.mock import patch

from apps.graph.management.commands.recover_graph import Command


@pytest.mark.unit
class TestRecoverGraphCommand:
    """recover_graph 管理命令测试"""

    def test_handle_without_force(self):
        """不带 --force 时，应以 force=False 调用恢复逻辑"""
        cmd = Command()
        stats = {'recovered': 3, 'dispatched_nodes': 1}
        with patch('apps.graph.tasks._recover_stuck_graph_docs',
                   return_value=stats) as mock_recover, \
                patch.object(cmd, 'stdout') as mock_stdout:
            cmd.handle(force=False)

        mock_recover.assert_called_once_with(force=False)
        # 应输出恢复结果（含回退数和派发数）
        assert any('恢复完成' in str(call) for call in mock_stdout.write.call_args_list)

    def test_handle_with_force(self):
        """带 --force 时，应以 force=True 调用恢复逻辑，跳过活跃标记检查"""
        cmd = Command()
        stats = {'recovered': 5, 'dispatched_nodes': 2}
        with patch('apps.graph.tasks._recover_stuck_graph_docs',
                   return_value=stats) as mock_recover, \
                patch.object(cmd, 'stdout') as mock_stdout:
            cmd.handle(force=True)

        mock_recover.assert_called_once_with(force=True)
        assert any('恢复完成' in str(call) for call in mock_stdout.write.call_args_list)

    def test_handle_when_no_stuck_docs(self):
        """无卡死文档时，应输出提示信息'没有发现卡死的图谱构建状态'"""
        cmd = Command()
        stats = {'recovered': 0, 'dispatched_nodes': 0}
        with patch('apps.graph.tasks._recover_stuck_graph_docs',
                   return_value=stats), \
                patch.object(cmd, 'stdout') as mock_stdout:
            cmd.handle(force=False)

        # 提取所有 write 调用的参数
        written = [str(call) for call in mock_stdout.write.call_args_list]
        assert any('没有发现卡死的图谱构建状态' in w for w in written)

    def test_add_arguments_has_force_flag(self):
        """add_arguments 应注册 --force 参数"""
        import argparse
        parser = argparse.ArgumentParser()
        Command().add_arguments(parser)
        args = parser.parse_args(['--force'])
        assert args.force is True

    def test_add_arguments_force_defaults_false(self):
        """--force 默认值应为 False"""
        import argparse
        parser = argparse.ArgumentParser()
        Command().add_arguments(parser)
        args = parser.parse_args([])
        assert args.force is False
