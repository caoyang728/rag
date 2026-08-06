"""
task_splitter（复杂任务拆分器）单元测试

覆盖：
- maybe_split 的 JSON 解析容错（纯 JSON / ```json 包裹 / 非法 / 空内容）
- execute_split 的依赖分层：无依赖并行、有依赖串行、循环依赖保护
- 合并阶段 LLM 调用、QaRecord 落库（_persist_qa）

全部 mock LLM / 混合检索 / 落库，不依赖外部服务与数据库。

注意：task_splitter 模块顶层 `from apps.llm.factory import get_llm` 在 import 时
就把名称绑定进本模块命名空间，因此必须 patch 使用点 apps.agent.task_splitter.get_llm，
patch 定义处（apps.llm.factory.get_llm）不会生效。
"""
import pytest
from unittest.mock import patch, MagicMock

from apps.agent import task_splitter

pytestmark = pytest.mark.unit


def _make_chat_side_effect(sub_answers, merge_answer='merged'):
    """按子问题文本返回对应答案的 chat side_effect

    由于无依赖子任务在同一 ThreadPool 批次内并发执行，chat 调用顺序不确定，
    不能依赖调用次序，必须根据入参（子问题文本）返回对应答案，避免测试抖动。
    """
    def chat_side_effect(msgs, **kwargs):
        # _run 的 messages 由 build_qa_messages 生成，user message 内容即子问题文本；
        # 合并调用的 msgs[0] 是 TASK_MERGE_SYSTEM，不命中 sub_answers，自然落到合并结果
        if msgs and msgs[0].get('content') in sub_answers:
            return {'content': sub_answers[msgs[0]['content']]}
        return {'content': merge_answer}
    return chat_side_effect


class TestMaybeSplit:
    """maybe_split：LLM 判断是否需要拆分"""

    @patch.object(task_splitter, 'get_llm')
    def test_maybe_split_when_plain_json_then_parsed(self, mock_get_llm):
        """LLM 返回纯 JSON 字符串应被正确解析"""
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {
            'content': '{"need_split": true, "sub_tasks": [{"index": 1, "question": "子问题"}]}'
        }
        mock_get_llm.return_value = mock_llm

        data = task_splitter.maybe_split('测试问题')

        assert data['need_split'] is True
        assert data['sub_tasks'][0]['question'] == '子问题'

    @patch.object(task_splitter, 'get_llm')
    def test_maybe_split_when_json_block_then_parsed(self, mock_get_llm):
        """LLM 偶尔用 ```json 包裹输出，需先剥掉代码块标记再解析"""
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {
            'content': '```json\n{"need_split": false, "reason": "简单问题不需要拆分"}\n```'
        }
        mock_get_llm.return_value = mock_llm

        data = task_splitter.maybe_split('简单问题')

        assert data['need_split'] is False
        assert data['reason'] == '简单问题不需要拆分'

    @patch.object(task_splitter, 'get_llm')
    def test_maybe_split_when_invalid_json_then_returns_none(self, mock_get_llm):
        """LLM 输出非 JSON 时降级为不拆分，避免解析异常击穿调用链"""
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {'content': '这根本不是 JSON'}
        mock_get_llm.return_value = mock_llm

        data = task_splitter.maybe_split('测试问题')

        assert data == {'need_split': False, 'reason': 'llm output invalid json'}

    @patch.object(task_splitter, 'get_llm')
    def test_maybe_split_when_empty_then_returns_none(self, mock_get_llm):
        """LLM 返回空 content 时同样走 json.loads 失败分支，返回不拆分"""
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {'content': ''}
        mock_get_llm.return_value = mock_llm

        data = task_splitter.maybe_split('测试问题')

        assert data == {'need_split': False, 'reason': 'llm output invalid json'}

    @patch.object(task_splitter, 'get_llm')
    def test_maybe_split_then_calls_llm_with_template(self, mock_get_llm):
        """验证 LLM 调用使用了任务拆分专用 system 提示词与固定采样参数"""
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {'content': '{"need_split": false}'}
        mock_get_llm.return_value = mock_llm

        from apps.llm.prompts import TASK_SPLIT_SYSTEM
        task_splitter.maybe_split('测试问题')

        msgs = mock_llm.chat.call_args[0][0]
        assert msgs[0] == {'role': 'system', 'content': TASK_SPLIT_SYSTEM}
        assert msgs[1]['role'] == 'user'
        assert '测试问题' in msgs[1]['content']
        # 拆分判断需要稳定输出，temperature 固定为 0
        assert mock_llm.chat.call_args[1]['temperature'] == 0.0
        assert mock_llm.chat.call_args[1]['max_tokens'] == 800


class TestExecuteSplit:
    """execute_split：执行子任务并按依赖分层合并"""

    @patch('apps.agent.executor._persist_qa')
    @patch.object(task_splitter, 'get_llm')
    def test_execute_split_when_empty_subtasks_then_returns_empty(self, mock_get_llm, mock_persist):
        """sub_tasks 为空时直接返回空提示，不触发任何 LLM 调用"""
        result = task_splitter.execute_split(None, MagicMock(), 'q', {'sub_tasks': []})

        assert result['answer'] == '（任务拆分为空）'
        assert result['chunks'] == []
        assert result['is_hit_cache'] is False
        assert result['qa_id'] is None
        mock_get_llm.assert_not_called()
        mock_persist.assert_not_called()

    @patch('apps.agent.executor._persist_qa')
    @patch('apps.llm.prompts.build_qa_messages')
    @patch('apps.retrieval.hybrid.hybrid_search')
    @patch.object(task_splitter, 'get_llm')
    def test_execute_split_when_no_deps_then_executes_all(self, mock_get_llm, mock_hybrid,
                                   mock_build_qa, mock_persist):
        """无依赖的子任务应全部执行并汇总答案（同一批并行执行）"""
        split = {'sub_tasks': [
            {'index': 1, 'question': '子问题1', 'depends_on': []},
            {'index': 2, 'question': '子问题2', 'depends_on': []},
        ]}
        mock_llm = MagicMock()
        mock_llm.chat.side_effect = _make_chat_side_effect(
            {'子问题1': 'ans1', '子问题2': 'ans2'})
        mock_get_llm.return_value = mock_llm
        mock_hybrid.return_value = {'chunks': [{'chunk_id': 'c1'}]}
        # _run 内的 build_qa_messages 把子问题文本放进 user message，供 chat side_effect 区分
        mock_build_qa.side_effect = lambda q, chunks: [{'role': 'user', 'content': q}]
        mock_persist.return_value = MagicMock(id=7)

        user = MagicMock()
        session = MagicMock(turn_count=0)
        result = task_splitter.execute_split(user, session, '总问题', split,
                                            root_types=['company_doc'])

        # 两个子任务均执行了检索
        assert mock_hybrid.call_count == 2
        # 检索携带 root_types 与 rerank 参数
        for call in mock_hybrid.call_args_list:
            assert call[1]['root_types'] == ['company_doc']
            assert call[1]['do_rerank'] is True
        # 子答案按 index 汇总
        assert result['sub_answers'] == {1: 'ans1', 2: 'ans2'}
        # 合并后落库
        assert result['answer'] == 'merged'
        assert result['qa_id'] == 7
        mock_persist.assert_called_once()

    @patch('apps.agent.executor._persist_qa')
    @patch('apps.llm.prompts.build_qa_messages')
    @patch('apps.retrieval.hybrid.hybrid_search')
    @patch.object(task_splitter, 'get_llm')
    def test_execute_split_when_with_deps_then_resolves_order(self, mock_get_llm, mock_hybrid,
                                     mock_build_qa, mock_persist):
        """任务 B 依赖任务 A 时，B 必须在 A 完成后的下一批才执行"""
        split = {'sub_tasks': [
            {'index': 1, 'question': '子问题1', 'depends_on': []},
            {'index': 2, 'question': '子问题2', 'depends_on': [1]},
        ]}
        mock_llm = MagicMock()
        mock_llm.chat.side_effect = _make_chat_side_effect(
            {'子问题1': 'ans1', '子问题2': 'ans2'})
        mock_get_llm.return_value = mock_llm
        mock_build_qa.side_effect = lambda q, chunks: [{'role': 'user', 'content': q}]
        mock_persist.return_value = MagicMock(id=7)
        # 记录检索顺序：依赖未满足时 A 单独先跑，B 只能在 A 完成后的批次出现
        order = []

        def hybrid_side_effect(query, user, root_types=None, **kwargs):
            order.append(query)
            return {'chunks': []}

        mock_hybrid.side_effect = hybrid_side_effect

        result = task_splitter.execute_split(MagicMock(), MagicMock(turn_count=0),
                                             '总问题', split)

        assert order == ['子问题1', '子问题2']
        assert result['sub_answers'] == {1: 'ans1', 2: 'ans2'}
        assert result['answer'] == 'merged'

    @patch('apps.agent.executor._persist_qa')
    @patch.object(task_splitter, 'get_llm')
    def test_execute_split_when_circular_deps_then_returns_error(self, mock_get_llm, mock_persist):
        """循环依赖（A↔B）时不能死循环：当前批为空即终止，仍走合并与落库"""
        split = {'sub_tasks': [
            {'index': 1, 'question': '子问题1', 'depends_on': [2]},
            {'index': 2, 'question': '子问题2', 'depends_on': [1]},
        ]}
        mock_llm = MagicMock()
        mock_llm.chat.side_effect = _make_chat_side_effect({})
        mock_get_llm.return_value = mock_llm
        mock_persist.return_value = MagicMock(id=9)

        result = task_splitter.execute_split(MagicMock(), MagicMock(turn_count=0),
                                             '总问题', split)

        # 任一依赖都无法满足，任何子任务都不应执行
        # 但合并与落库仍应完成，保证调用方拿到一个可用答案
        assert result['answer'] == 'merged'
        assert result['qa_id'] == 9
        assert mock_llm.chat.call_count == 1  # 仅合并一次
        mock_persist.assert_called_once()

    @patch('apps.agent.executor._persist_qa')
    @patch('apps.llm.prompts.build_qa_messages')
    @patch('apps.retrieval.hybrid.hybrid_search')
    @patch.object(task_splitter, 'get_llm')
    def test_execute_split_then_merges_answers(self, mock_get_llm, mock_hybrid,
                                          mock_build_qa, mock_persist):
        """合并调用应使用合并专用 system 提示词，并携带全部子问题的答案"""
        split = {'sub_tasks': [
            {'index': 1, 'question': '子问题1', 'depends_on': []},
            {'index': 2, 'question': '子问题2', 'depends_on': []},
        ]}
        mock_llm = MagicMock()
        mock_llm.chat.side_effect = _make_chat_side_effect(
            {'子问题1': 'ans1', '子问题2': 'ans2'}, merge_answer='综合回答')
        mock_get_llm.return_value = mock_llm
        mock_hybrid.return_value = {'chunks': []}
        mock_build_qa.side_effect = lambda q, chunks: [{'role': 'user', 'content': q}]
        mock_persist.return_value = MagicMock(id=7)

        from apps.llm.prompts import TASK_MERGE_SYSTEM
        result = task_splitter.execute_split(MagicMock(), MagicMock(turn_count=0),
                                             '总问题', split)

        assert result['answer'] == '综合回答'
        # 最后一次 chat 调用即合并调用
        merge_call = mock_llm.chat.call_args_list[-1]
        msgs = merge_call.args[0]
        assert msgs[0] == {'role': 'system', 'content': TASK_MERGE_SYSTEM}
        # 合并 prompt 中应能看到每个子问题及其答案
        user_content = msgs[1]['content']
        assert '子问题 1: 子问题1' in user_content
        assert '答案：ans1' in user_content
        assert '子问题 2: 子问题2' in user_content
        assert '答案：ans2' in user_content
        assert merge_call.kwargs['temperature'] == 0.3
