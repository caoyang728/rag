"""
工作流编排器（planner）单元测试

覆盖：
- validate_dag：节点数上限 / id 唯一 / 类型合法 / 工具名存在 / depends_on 引用 / 循环依赖
- maybe_plan：LLM 输出容错解析（纯 JSON / ```json 包裹 / 非法 / 空）、LLM 异常降级、
  DAG 校验失败降级为"不拆分"（复杂问题回退现有 ReAct 链路）

全部 mock LLM 与工具注册表，不依赖外部服务与数据库。
"""
import pytest
from unittest.mock import patch, MagicMock

from apps.agent.workflow import planner

pytestmark = pytest.mark.unit


def _valid_nodes():
    """合法的两节点 DAG：research1（无依赖）→ research2（依赖 research1）"""
    return [
        {'id': 'r1', 'name': '研究1', 'type': 'research', 'question': '子问题1', 'depends_on': []},
        {'id': 'r2', 'name': '研究2', 'type': 'research', 'question': '子问题2', 'depends_on': ['r1']},
    ]


class TestValidateDag:
    """validate_dag：节点 DAG 合法性校验"""

    def test_validate_dag_when_valid_then_ok(self):
        ok, errors = planner.validate_dag(_valid_nodes())
        assert ok is True
        assert errors == []

    def test_validate_dag_when_empty_then_ok(self):
        """空节点列表视为合法（maybe_plan 对无节点输出走不拆分分支）"""
        ok, errors = planner.validate_dag([])
        assert ok is True

    def test_validate_dag_when_over_limit_then_error(self):
        """节点数超过上限应报错（防编排器输出失控 DAG）"""
        nodes = [{'id': f'n{i}', 'name': f'N{i}', 'type': 'research',
                  'question': f'q{i}', 'depends_on': []} for i in range(11)]
        ok, errors = planner.validate_dag(nodes, max_nodes=10)
        assert ok is False
        assert any('上限' in e for e in errors)

    def test_validate_dag_when_duplicate_id_then_error(self):
        nodes = _valid_nodes() + [{'id': 'r1', 'name': '重复', 'type': 'research',
                                   'question': 'q', 'depends_on': []}]
        ok, errors = planner.validate_dag(nodes)
        assert ok is False
        assert any('重复' in e for e in errors)

    def test_validate_dag_when_unknown_tool_then_error(self):
        """编排器幻觉出不存在的工具时应校验失败（防止运行时工具缺失）"""
        nodes = [{'id': 't1', 'name': '工具', 'type': 'tool',
                  'tool_name': '不存在的工具', 'params': {}, 'depends_on': []}]
        ok, errors = planner.validate_dag(nodes)
        assert ok is False
        assert any('不存在' in e for e in errors)

    def test_validate_dag_when_unknown_dependency_then_error(self):
        nodes = _valid_nodes() + [{'id': 't1', 'name': '工具', 'type': 'tool',
                                   'tool_name': 'calculator', 'params': {'expr': '1+1'},
                                   'depends_on': ['ghost']}]
        ok, errors = planner.validate_dag(nodes)
        assert ok is False
        assert any('ghost' in e for e in errors)

    def test_validate_dag_when_cycle_then_error(self):
        """循环依赖（r1↔r2）必须被识别，防止执行引擎死循环"""
        nodes = [
            {'id': 'r1', 'name': 'A', 'type': 'research', 'question': 'q1', 'depends_on': ['r2']},
            {'id': 'r2', 'name': 'B', 'type': 'research', 'question': 'q2', 'depends_on': ['r1']},
        ]
        ok, errors = planner.validate_dag(nodes)
        assert ok is False
        assert any('循环' in e for e in errors)

    def test_validate_dag_when_tool_valid_then_ok(self):
        """calculator 是注册表内工具，应通过校验"""
        nodes = [{'id': 't1', 'name': '计算', 'type': 'tool',
                  'tool_name': 'calculator', 'params': {'expr': '1+1'}, 'depends_on': []}]
        ok, errors = planner.validate_dag(nodes)
        assert ok is True


class TestMaybePlan:
    """maybe_plan：LLM 判断是否需要工作流并产出节点 DAG"""

    def _patch_llm(self, content):
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {'content': content}
        patcher = patch.object(planner, 'get_llm', return_value=mock_llm)
        patcher.start()
        return patcher, mock_llm

    def test_maybe_plan_when_complex_then_nodes(self):
        import json as _json
        p, _ = self._patch_llm(_json.dumps(
            {'need_workflow': True, 'reason': '复杂', 'nodes': _valid_nodes()},
            ensure_ascii=False))
        try:
            data = planner.maybe_plan('复杂问题')
        finally:
            p.stop()
        assert data['need_workflow'] is True
        assert len(data['nodes']) == 2

    def test_maybe_plan_when_simple_then_no_workflow(self):
        p, _ = self._patch_llm('{"need_workflow": false, "reason": "简单问题"}')
        try:
            data = planner.maybe_plan('简单问题')
        finally:
            p.stop()
        assert data['need_workflow'] is False

    def test_maybe_plan_when_json_block_then_parsed(self):
        """兼容 ```json 包裹输出（与 task_splitter 解析策略一致）"""
        p, _ = self._patch_llm('```json\n{"need_workflow": true, "reason": "r", "nodes": []}\n```')
        try:
            data = planner.maybe_plan('问题')
        finally:
            p.stop()
        assert data['need_workflow'] is True
        assert data['nodes'] == []

    def test_maybe_plan_when_invalid_json_then_degrade(self):
        p, _ = self._patch_llm('这根本不是 JSON')
        try:
            data = planner.maybe_plan('问题')
        finally:
            p.stop()
        assert data == {'need_workflow': False, 'reason': 'planner output invalid json'}

    def test_maybe_plan_when_empty_content_then_degrade(self):
        p, _ = self._patch_llm('')
        try:
            data = planner.maybe_plan('问题')
        finally:
            p.stop()
        assert data['need_workflow'] is False

    def test_maybe_plan_when_llm_error_then_degrade(self):
        """LLM 调用异常时降级为不拆分（可用性优先，回退现有 ReAct 链路）"""
        mock_llm = MagicMock()
        mock_llm.chat.side_effect = RuntimeError('llm down')
        with patch.object(planner, 'get_llm', return_value=mock_llm):
            data = planner.maybe_plan('问题')
        assert data['need_workflow'] is False
        assert data['reason'] == 'planner llm error'

    def test_maybe_plan_when_dag_invalid_then_degrade(self):
        """DAG 校验失败（如幻觉出不存在的工具）→ 拒绝进入工作流，降级走现有链路"""
        import json as _json
        nodes = [{'id': 't1', 'name': 'T', 'type': 'tool',
                  'tool_name': '幻觉工具', 'params': {}, 'depends_on': []}]
        p, _ = self._patch_llm(_json.dumps(
            {'need_workflow': True, 'reason': 'r', 'nodes': nodes}, ensure_ascii=False))
        try:
            data = planner.maybe_plan('问题')
        finally:
            p.stop()
        assert data['need_workflow'] is False
        assert data['reason'].startswith('dag invalid')

    def test_maybe_plan_then_uses_workflow_prompt(self):
        """验证编排器使用工作流专用 system 提示词与稳定采样参数"""
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {'content': '{"need_workflow": false, "reason": "s"}'}
        with patch.object(planner, 'get_llm', return_value=mock_llm):
            planner.maybe_plan('测试问题', max_nodes=8)

        from apps.llm.prompts.workflow import WORKFLOW_PLAN_SYSTEM
        msgs = mock_llm.chat.call_args[0][0]
        assert msgs[0] == {'role': 'system', 'content': WORKFLOW_PLAN_SYSTEM}
        assert msgs[1]['role'] == 'user'
        assert '测试问题' in msgs[1]['content']
        assert mock_llm.chat.call_args[1]['temperature'] == 0.0
