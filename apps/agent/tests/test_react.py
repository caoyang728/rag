"""
react（ReAct Agent 循环）单元测试

覆盖：
- _make_filtered_event：content_filtered 事件构造（带/不带 category）
- _build_tool_messages：工具调用消息回填（OpenAI 协议）与超长结果截断
- _collect_citations：知识库引用收集（按文档合并 / chunk 去重 / 非 knowledge_search 忽略）
- parse_tool_arguments：LLM 工具参数 JSON 容错解析（裸 JSON / ```json 代码块 / 非法兜底）
- _execute_tool_calls：单批工具调用执行（参数解析 + call_id 回填）
- agent_ask 同步 ReAct 循环：
  - 工具调用流程：LLM 决策 → 参数解析 → 工具执行 → 结果回填 → 再决策
  - 首轮直答（无工具调用）
  - 工具执行失败（ok=False）记录但不中断循环，由 LLM 决定重试
  - 最大轮数保护（MAX_TOOL_ROUNDS）强制 LLM 总结
  - 最后一轮仍返回 tool_calls 时触发 for-else 防御性后备块强制总结
  - 自定义工具注册进真实 ToolRegistry 后参与循环
- agent_ask_stream 流式 ReAct 循环（含敏感词审查）：
  - 输入侧 block 拦截（命中即停，不浪费 LLM 调用）
  - 输入侧 get_sensitive_filter 异常 → 跳过输入审查继续主流程
  - 流式审查器 new_state 初始化失败 → 输出侧审查降级，delta 原样下发
  - 正常流：工具调用轮发 tool_call/tool_result，答案轮流式输出
  - 输出侧流式命中 block：立即中断生成，done 标记 is_filtered
  - LLM 流式调用抛异常：发出 error 事件而非让生成器崩溃
  - 主循环 GeneratorExit：捕获并 re-raise，close() 不抛异常
  - 主循环异常时 flush 残留安全内容再发 error
  - 超长工具结果在 tool_result 事件中截断预览（完整结果仍落库）
  - 收尾 flush 命中 block：发 content_filtered 事件
  - 收尾 flush 抛异常：仅记录日志，流程正常收尾
  - 全程无 delta（首帧即 finish）：补 first_token 事件再 done
  - 异常轮 flush 无输出时 first_token 由收尾逻辑兜底补发

全部 mock LLM / 工具注册表 / 记忆 / 敏感词过滤器，不依赖外部服务与数据库。

注意：react 模块顶层 `from apps.llm.factory import get_llm` 等 import 在模块加载时
就把名称绑定进 react 命名空间，因此必须 patch 使用点 apps.agent.react.*，
patch 定义处（apps.llm.factory.get_llm 等）不会生效。
sensitive_filter 为函数内 import，patch 定义处即可生效。
"""
import pytest
from contextlib import ExitStack
from unittest.mock import patch, MagicMock, PropertyMock  # noqa: F401

from apps.agent import react

pytestmark = pytest.mark.unit


def _enter_env(mock_llm, mock_registry, mock_mm, mock_sf=None):
    """进入 agent_ask / agent_ask_stream 所需的全部 mock 上下文

    用 ExitStack 统一管理，避免每个用例写一大串嵌套 with。
    mock_sf 仅流式用例需要（输入侧与输出侧敏感词审查共用同一实例）。
    """
    stack = ExitStack()
    stack.enter_context(patch.object(react, 'get_llm', return_value=mock_llm))
    stack.enter_context(patch.object(react, 'get_default_registry', return_value=mock_registry))
    stack.enter_context(patch.object(react, 'MemoryManager', return_value=mock_mm))
    stack.enter_context(patch.object(react, 'build_agent_messages', return_value=[]))
    if mock_sf is not None:
        # react 内部通过 from apps.security.sensitive_filter import get_sensitive_filter
        # 在函数内引用，属于调用时导入，patch 定义处即可生效
        stack.enter_context(patch('apps.security.sensitive_filter.get_sensitive_filter',
                                  return_value=mock_sf))
    return stack


def _default_sf():
    """默认放行的敏感词过滤器 mock：check 无命中、feed/flush 原样透传

    new_state 必须返回真值 dict：react 以 `if sf and filter_state` 判断审查是否启用，
    空 dict 是假值会导致审查分支被跳过。
    """
    mock_sf = MagicMock()
    mock_sf.check.return_value = []
    mock_sf.new_state.return_value = {'buffer': ''}
    mock_sf.feed.side_effect = lambda state, delta: ([delta], None)
    mock_sf.flush.return_value = ([], None)
    return mock_sf


def _run_stream(mock_llm, mock_sf=None, registry_execute=None):
    """驱动 agent_ask_stream 消费全部事件（返回事件列表）

    registry_execute 可选：覆盖默认的工具执行结果，用于需要超长结果等场景。
    """
    mock_registry = MagicMock()
    if registry_execute is not None:
        mock_registry.execute.return_value = registry_execute
    else:
        mock_registry.execute.return_value = {
            'result': '2', 'ok': True, 'meta': {}, 'latency_ms': 2, 'tool_name': 'calculator'}
    mock_mm = MagicMock()
    mock_mm.load_context.return_value = {'memory_block': ''}

    with _enter_env(mock_llm, mock_registry, mock_mm, mock_sf=mock_sf):
        return list(react.agent_ask_stream(MagicMock(), '测试问题', MagicMock()))


class TestMakeFilteredEvent:
    """_make_filtered_event：content_filtered 事件构造"""

    def test_make_filtered_event_when_has_category_then_uses_category(self):
        """命中词带分类时透传 category（供运营分析，不暴露命中词本身）"""
        hit = MagicMock()
        hit.category = 'porn'
        ev = react._make_filtered_event(hit)

        assert ev['type'] == 'content_filtered'
        assert ev['reason'] == '检测到违规内容，已拦截'
        assert ev['category'] == 'porn'

    def test_make_filtered_event_when_no_category_then_falls_back(self):
        """无 category 属性时兜底为 'other'，避免 getattr 抛异常"""
        class _Hit:  # 无 category 属性的普通对象
            pass

        ev = react._make_filtered_event(_Hit())
        assert ev['category'] == 'other'


class TestBuildToolMessages:
    """_build_tool_messages：工具调用消息回填"""

    def test_build_tool_messages_when_normal_then_correct_format(self):
        """按 OpenAI 协议：assistant 带 tool_calls，随后每条 tool result 一一对应"""
        tool_calls = [{'id': 'c1', 'name': 'calculator', 'arguments': '{"expr": "1+1"}'}]
        results = [{'result': '2', 'ok': True, 'meta': {}}]

        msgs = react._build_tool_messages(tool_calls, results)

        # assistant message：必须保留 tool_calls 原始结构（OpenAI 协议要求）
        assert msgs[0]['role'] == 'assistant'
        assert msgs[0]['content'] is None
        tc = msgs[0]['tool_calls'][0]
        assert tc['id'] == 'c1'
        assert tc['type'] == 'function'
        assert tc['function']['name'] == 'calculator'
        assert tc['function']['arguments'] == '{"expr": "1+1"}'
        # tool message：通过 tool_call_id 与调用关联
        assert msgs[1]['role'] == 'tool'
        assert msgs[1]['tool_call_id'] == 'c1'
        assert msgs[1]['content'] == '2'

    def test_build_tool_messages_when_long_result_then_truncated(self):
        """超长工具结果必须截断，防止 context 膨胀拖垮后续轮次"""
        long_result = 'x' * (react.MAX_TOOL_RESULT_CHARS + 100)
        tool_calls = [{'id': 'c1', 'name': 'tool', 'arguments': '{}'}]

        msgs = react._build_tool_messages(tool_calls, [{'result': long_result, 'ok': True}])

        content = msgs[1]['content']
        assert content.startswith('x' * react.MAX_TOOL_RESULT_CHARS)
        assert content.endswith('...（结果已截断）')


class TestCollectCitations:
    """_collect_citations：从工具调用链收集知识库引用"""

    def test_collect_citations_when_multiple_chunks_then_merges_by_doc(self):
        """同一文档多次命中应合并引用，同一 chunk 跨调用命中应去重"""
        traces = [
            {'tool_name': 'knowledge_search', 'meta': {'chunks': [
                {'chunk_id': 'c1', 'doc_title': 'Doc A', 'section_path': 's1', 'page_number': 3},
                {'chunk_id': 'c2', 'doc_title': 'Doc A', 'section_path': 's2', 'page_number': 3},
            ]}},
            # 第二次调用重复命中 c1：应去重；c3 属于新文档
            {'tool_name': 'knowledge_search', 'meta': {'chunks': [
                {'chunk_id': 'c1', 'doc_title': 'Doc A', 'section_path': 's1', 'page_number': 3},
                {'chunk_id': 'c3', 'doc_title': 'Doc B', 'section_path': None, 'page_number': None},
            ]}},
            # 非 knowledge_search 工具即使带 chunks 元数据也应被忽略
            {'tool_name': 'calculator', 'meta': {'chunks': [
                {'chunk_id': 'c9', 'doc_title': 'Doc X'},
            ]}},
        ]

        citations, all_chunks = react._collect_citations(traces)

        assert len(all_chunks) == 3  # c1, c2, c3
        assert len(citations) == 2

        doc_a = [c for c in citations if c['doc_title'] == 'Doc A'][0]
        assert doc_a['chunk_ids'] == ['c1', 'c2']
        # section 来自 set，顺序不保证，按排序后比较
        assert sorted(doc_a['section'].split(', ')) == ['s1', 's2']
        assert doc_a['page'] == [3]

        doc_b = [c for c in citations if c['doc_title'] == 'Doc B'][0]
        assert doc_b['chunk_ids'] == ['c3']
        assert doc_b['section'] == ''
        assert doc_b['page'] == []

    def test_collect_citations_when_no_knowledge_tool_then_returns_empty(self):
        """没有任何 knowledge_search 调用时返回空引用"""
        citations, all_chunks = react._collect_citations([
            {'tool_name': 'web_search', 'meta': {}},
            {'tool_name': 'calculator', 'meta': {}},
        ])
        assert citations == []
        assert all_chunks == []


class TestParseToolArguments:
    """parse_tool_arguments：LLM 工具参数 JSON 容错解析（ReAct 循环复用）"""

    def test_parse_tool_arguments_when_plain_json_then_parsed(self):
        from apps.agent.tools import parse_tool_arguments
        assert parse_tool_arguments('{"query": "x"}') == {'query': 'x'}

    def test_parse_tool_arguments_when_json_block_then_parsed(self):
        """模型偶尔用 ```json 包裹 arguments，需剥掉代码块标记"""
        from apps.agent.tools import parse_tool_arguments
        assert parse_tool_arguments('```json\n{"a": 1}\n```') == {'a': 1}

    def test_parse_tool_arguments_when_invalid_then_returns_empty(self):
        """解析失败返回空 dict，避免阻断 Agent 循环"""
        from apps.agent.tools import parse_tool_arguments
        assert parse_tool_arguments('not json') == {}
        assert parse_tool_arguments('') == {}


class TestExecuteToolCalls:
    """_execute_tool_calls：单批工具调用执行"""

    def test_execute_tool_calls_when_normal_then_parses_and_attaches_call_id(self):
        """arguments 从 JSON 解析为 dict，且 call_id 被回填到结果"""
        from apps.agent.tools import ToolContext
        registry = MagicMock()
        registry.execute.return_value = {'result': 'ok', 'ok': True, 'meta': {}, 'latency_ms': 1}
        tool_calls = [{'id': 'c1', 'name': 'calculator', 'arguments': '{"expr": "1+1"}'}]

        results = react._execute_tool_calls(tool_calls, ToolContext(), registry)

        assert results[0]['call_id'] == 'c1'
        args = registry.execute.call_args[0]
        assert args[0] == 'calculator'
        assert args[1] == {'expr': '1+1'}
        assert isinstance(args[2], ToolContext)


class TestReactAgentAsk:
    """agent_ask：同步 ReAct 循环"""

    @staticmethod
    def _run(chat_side_effect, execute_result=None):
        """按给定 LLM 行为执行 agent_ask，返回 (result, mock_llm, mock_registry)"""
        mock_llm = MagicMock()
        mock_llm.chat.side_effect = chat_side_effect
        mock_registry = MagicMock()
        if execute_result is not None:
            mock_registry.execute.return_value = execute_result
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        with _enter_env(mock_llm, mock_registry, mock_mm):
            result = react.agent_ask(MagicMock(), '测试问题', MagicMock(),
                                     root_types=['company_doc'])
        return result, mock_llm, mock_registry

    def test_agent_ask_when_tool_call_then_executes_tool(self):
        """LLM 先请求调用工具，拿到结果后回填并给出最终答案"""
        tool_call = {'id': 'c1', 'name': 'calculator', 'arguments': '{"expr": "1+1"}'}
        calls = {'n': 0}

        def chat_side_effect(msgs, **kwargs):
            calls['n'] += 1
            # 第一轮只返回 tool_calls（无文本）；第二轮返回最终答案
            if calls['n'] == 1:
                return {'content': None, 'tool_calls': [tool_call],
                        'latency_ms': 10, 'prompt_tokens': 5, 'completion_tokens': 2, 'cost': 0.001}
            return {'content': '答案是 2',
                    'latency_ms': 20, 'prompt_tokens': 8, 'completion_tokens': 4, 'cost': 0.002}

        result, mock_llm, mock_registry = self._run(chat_side_effect, execute_result={
            'result': '2', 'ok': True, 'meta': {}, 'latency_ms': 3, 'tool_name': 'calculator'})

        assert result['answer'] == '答案是 2'
        assert len(result['tool_traces']) == 1
        trace = result['tool_traces'][0]
        assert trace['tool_name'] == 'calculator'
        assert trace['ok'] is True
        assert trace['round'] == 1
        # 参数已从 JSON 解析为 dict 再执行
        args = mock_registry.execute.call_args[0]
        assert args[0] == 'calculator'
        assert args[1] == {'expr': '1+1'}
        # 工具轮 + 最终答案轮，共两次 LLM 调用
        assert mock_llm.chat.call_count == 2
        # LLM 统计跨轮累加
        assert result['llm_stats']['tokens_prompt'] == 13
        assert result['llm_stats']['tokens_completion'] == 6

    def test_agent_ask_when_no_tool_call_then_direct_answer(self):
        """LLM 首轮直接给出文本答案，不进入工具调用"""
        result, mock_llm, mock_registry = self._run(
            lambda msgs, **kwargs: {'content': '直接回答'})

        assert result['answer'] == '直接回答'
        assert result['tool_traces'] == []
        assert mock_llm.chat.call_count == 1
        mock_registry.execute.assert_not_called()

    def test_agent_ask_when_tool_fails_then_records_and_continues(self):
        """工具执行失败（ok=False）不应中断循环：错误记录在 tool_traces，LLM 可据此重试"""
        tool_call = {'id': 'c1', 'name': 'web_search', 'arguments': '{"query": "x"}'}
        calls = {'n': 0}

        def chat_side_effect(msgs, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                return {'content': None, 'tool_calls': [tool_call]}
            return {'content': '重试后回答'}

        result, mock_llm, _ = self._run(chat_side_effect, execute_result={
            'result': '工具执行失败: Timeout: 连接超时', 'ok': False, 'meta': {},
            'latency_ms': 0, 'tool_name': 'web_search'})

        assert result['tool_traces'][0]['ok'] is False
        assert '连接超时' in result['tool_traces'][0]['result']
        # 失败后 LLM 继续收到结果并完成回答，循环未被击穿
        assert result['answer'] == '重试后回答'
        assert mock_llm.chat.call_count == 2

    def test_agent_ask_when_max_rounds_then_forces_final_answer(self):
        """达到 MAX_TOOL_ROUNDS 仍未给出答案时强制 LLM 总结，防止无限调用工具"""
        tool_call = {'id': 'c1', 'name': 'calculator', 'arguments': '{}'}
        calls = {'n': 0}

        def chat_side_effect(msgs, **kwargs):
            calls['n'] += 1
            if calls['n'] <= react.MAX_TOOL_ROUNDS:
                # 模拟 LLM 一直要调用工具、不肯收手
                return {'content': None, 'tool_calls': [tool_call],
                        'latency_ms': 0, 'prompt_tokens': 1, 'completion_tokens': 1, 'cost': 0}
            # 强制总结轮（无 tools 参数）终于给出答案
            return {'content': '强制总结答案'}

        result, mock_llm, _ = self._run(chat_side_effect, execute_result={
            'result': '2', 'ok': True, 'meta': {}, 'latency_ms': 1, 'tool_name': 'calculator'})

        assert result['answer'] == '强制总结答案'
        assert mock_llm.chat.call_count == react.MAX_TOOL_ROUNDS + 1
        assert len(result['tool_traces']) == react.MAX_TOOL_ROUNDS
        # 强制总结轮应不带 tools 参数
        last_call = mock_llm.chat.call_args_list[-1]
        assert 'tools' not in last_call.kwargs


class TestReactAgentAskStream:
    """agent_ask_stream：流式 ReAct 循环（含敏感词审查）"""

    def test_agent_ask_stream_when_input_block_then_stops_before_llm(self):
        """问题命中 block：直接 first_token + content_filtered + done，不浪费 LLM 调用"""
        mock_sf = MagicMock()
        hit = MagicMock()
        hit.action = 'block'
        hit.word = '违禁词'
        hit.category = 'porn'
        mock_sf.check.return_value = [hit]

        mock_llm = MagicMock()
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        with _enter_env(mock_llm, mock_registry, mock_mm, mock_sf=mock_sf):
            events = list(react.agent_ask_stream(MagicMock(), '问题', MagicMock()))

        types = [e['type'] for e in events]
        assert types == ['first_token', 'content_filtered', 'done']
        filtered = events[1]
        assert filtered['category'] == 'porn'
        done = events[-1]
        assert done['is_filtered'] is True
        assert done['filter_reason'] == 'input:违禁词'
        assert done['answer'] == ''
        # 输入侧拦截不应触发任何 LLM / 工具 / 记忆加载
        mock_llm.stream.assert_not_called()
        mock_registry.execute.assert_not_called()
        mock_mm.load_context.assert_not_called()

    def test_agent_ask_stream_when_normal_then_streams_tool_and_answer(self):
        """正常流：工具调用轮发 tool_call/tool_result，答案轮流式输出，done 结束"""
        mock_sf = _default_sf()
        mock_llm = MagicMock()
        # 第一轮：流式无文本，finish 时携带 tool_calls；第二轮：直接输出最终答案
        mock_llm.stream.side_effect = [
            iter([
                {'delta': ''},
                {'finish': True, 'tool_calls': [
                    {'id': 'c1', 'name': 'calculator', 'arguments': '{"expr": "1+1"}'}],
                    'latency_ms': 10},
            ]),
            iter([
                {'delta': '答案是 '},
                {'delta': '2'},
                {'finish': True, 'latency_ms': 5},
            ]),
        ]

        events = _run_stream(mock_llm, mock_sf=mock_sf)

        types = [e['type'] for e in events]
        # 顺序：工具调用过程在前，答案流在后
        assert types[0] == 'tool_call'
        assert types[1] == 'tool_result'
        assert 'first_token' in types
        assert 'delta' in types
        assert types[-1] == 'done'

        tool_result = events[1]
        assert tool_result['ok'] is True
        assert tool_result['result_preview'] == '2'

        done = events[-1]
        assert done['answer'] == '答案是 2'
        assert done['is_filtered'] is False
        assert len(done['tool_traces']) == 1

    def test_agent_ask_stream_when_output_block_then_emits_filtered(self):
        """输出侧流式命中 block：立即中断生成，done 标记 is_filtered"""
        mock_sf = _default_sf()
        block_hit = MagicMock()
        block_hit.action = 'block'
        block_hit.word = '违禁词'
        block_hit.category = 'other'
        # 模拟审查器：含'违禁'的片段触发 block，其余原样放行
        mock_sf.feed.side_effect = lambda state, delta: (
            ([], block_hit) if '违禁' in delta else ([delta], None))

        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'delta': '正常内容'},
            {'delta': '包含违禁词的内容'},
            {'delta': '不应再输出的内容'},
            {'finish': True, 'latency_ms': 5},
        ])

        events = _run_stream(mock_llm, mock_sf=mock_sf)

        types = [e['type'] for e in events]
        assert types[0] == 'first_token'
        assert types[1] == 'delta'
        assert 'content_filtered' in types
        assert types[-1] == 'done'
        # block 之后的内容不再下发
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['正常内容']
        done = events[-1]
        assert done['is_filtered'] is True
        assert done['filter_reason'].startswith('output:')
        assert done['answer'] == '正常内容'

    def test_agent_ask_stream_when_stream_error_then_emits_error_event(self):
        """LLM 流式调用抛异常：发出 error 事件而非让生成器崩溃"""
        mock_sf = _default_sf()
        mock_llm = MagicMock()
        mock_llm.stream.side_effect = Exception('LLM down')

        events = _run_stream(mock_llm, mock_sf=mock_sf)

        assert events[-1]['type'] == 'error'
        assert 'LLM down' in events[-1]['detail']


class TestInputFilterAndFilterInit:
    """输入侧审查异常 / 流式审查器初始化失败（审查未启用时原样下发）"""

    def test_agent_ask_stream_when_input_filter_exception_then_continues(self):
        """输入侧 get_sensitive_filter 抛异常：跳过输入审查继续主流程"""
        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'delta': '正常输出'}, {'finish': True, 'latency_ms': 5},
        ])
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        # 输入侧与初始化共用同一个 patch：两处 except（293-294 / 350-351）都会被触发
        with patch('apps.security.sensitive_filter.get_sensitive_filter',
                   side_effect=Exception('sf down')), \
                patch.object(react, 'get_llm', return_value=mock_llm), \
                patch.object(react, 'get_default_registry', return_value=mock_registry), \
                patch.object(react, 'MemoryManager', return_value=mock_mm), \
                patch.object(react, 'build_agent_messages', return_value=[]):
            events = list(react.agent_ask_stream(MagicMock(), '测试问题', MagicMock()))

        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        # 审查未启用：delta 原样下发（else 分支）
        assert deltas == ['正常输出']
        assert events[-1]['type'] == 'done'
        assert events[-1]['is_filtered'] is False

    def test_agent_ask_stream_when_filter_init_fails_then_delta_raw(self):
        """输入检查正常但 new_state 初始化失败：仅输出侧审查降级，delta 原样下发"""
        mock_sf = _default_sf()
        mock_sf.new_state.side_effect = Exception('state init failed')

        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'delta': '内容'}, {'finish': True, 'latency_ms': 5},
        ])

        events = _run_stream(mock_llm, mock_sf=mock_sf)
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['内容']
        assert events[-1]['type'] == 'done'


class TestMaxRoundsForElse:
    """最后一轮不带 tools（360）+ for-else 防御性后备块（470-517）"""

    def test_agent_ask_when_tool_calls_at_last_round_then_triggers_for_else(self):
        """LLM 每轮都返回 tool_calls（含最后一轮），触发 for-else 强制总结"""
        tc = {'id': 'c1', 'name': 'calculator', 'arguments': '{"expr": "1"}'}
        # 第 1~5 轮全部返回 finish + tool_calls；第 5 轮（最后一轮）stream_kwargs={}，
        # 但 LLM 仍返回 tool_calls → 循环自然结束 → else 块强制总结
        rounds = [
            iter([{'finish': True, 'tool_calls': [tc], 'latency_ms': 1}])
            for _ in range(react.MAX_TOOL_ROUNDS)
        ]
        final = iter([{'delta': '强制总结答案'}, {'finish': True, 'latency_ms': 1}])

        mock_llm = MagicMock()
        mock_llm.stream.side_effect = rounds + [final]

        events = _run_stream(mock_llm, mock_sf=_default_sf())

        types = [e['type'] for e in events]
        assert types.count('tool_call') == react.MAX_TOOL_ROUNDS
        assert types.count('tool_result') == react.MAX_TOOL_ROUNDS
        done = events[-1]
        assert done['type'] == 'done'
        assert done['answer'] == '强制总结答案'
        assert len(done['tool_traces']) == react.MAX_TOOL_ROUNDS
        assert done['stats']['tool_rounds'] == react.MAX_TOOL_ROUNDS


class TestStreamLoopEdgeCases:
    """主循环 GeneratorExit / 异常 flush / 结果预览截断"""

    def test_agent_ask_stream_when_generator_exit_then_propagates(self):
        """客户端在答案流中途断开：GeneratorExit 被捕获并 re-raise，close() 不抛异常"""
        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'delta': '内容'}, {'finish': True, 'latency_ms': 5},
        ])
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        with _enter_env(mock_llm, mock_registry, mock_mm, mock_sf=_default_sf()):
            gen = react.agent_ask_stream(MagicMock(), '测试问题', MagicMock())
            ev = next(gen)  # first_token
            assert ev['type'] == 'first_token'
            ev = next(gen)  # delta
            assert ev['type'] == 'delta'
            gen.close()  # 不应向调用方抛异常（GeneratorExit 已在内部处理）

    def test_agent_ask_stream_when_stream_error_then_flushes_residual(self):
        """主循环 LLM 流异常：先 flush 残留安全内容再发 error 事件"""
        mock_sf = _default_sf()
        mock_sf.flush.return_value = (['残留内容'], None)

        mock_llm = MagicMock()
        mock_llm.stream.side_effect = Exception('LLM down')

        events = _run_stream(mock_llm, mock_sf=mock_sf)

        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['残留内容']
        assert events[-1]['type'] == 'error'
        assert 'LLM down' in events[-1]['detail']

    def test_tool_result_preview_truncated(self):
        """超长工具结果（>500 字符）在 tool_result 事件中截断预览"""
        long_result = 'x' * 600
        mock_llm = MagicMock()
        mock_llm.stream.side_effect = [
            iter([{'finish': True, 'tool_calls': [
                {'id': 'c1', 'name': 'calculator', 'arguments': '{"expr": "1"}'}],
                'latency_ms': 5}]),
            iter([{'delta': '答案'}, {'finish': True, 'latency_ms': 5}]),
        ]
        events = _run_stream(mock_llm, mock_sf=_default_sf(), registry_execute={
            'result': long_result, 'ok': True, 'meta': {}, 'latency_ms': 2,
            'tool_name': 'calculator'})

        tool_result = [e for e in events if e['type'] == 'tool_result'][0]
        assert tool_result['result_preview'] == 'x' * 500 + '...'
        # 完整结果仍保留在 tool_traces（供落库），仅预览截断
        done = events[-1]
        assert done['tool_traces'][0]['result'] == long_result


class TestFinalFlushAndTtfb:
    """最终 flush 命中 block / flush 异常 / 全程无 delta 补 first_token"""

    def test_agent_ask_stream_when_final_flush_block_then_emits_filtered(self):
        """收尾 flush 命中 block：发 content_filtered 事件，done 标记 is_filtered"""
        mock_sf = _default_sf()
        block_hit = MagicMock(action='block', word='尾词', category='other')
        mock_sf.flush.return_value = ([], block_hit)

        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'delta': '内容'}, {'finish': True, 'latency_ms': 5},
        ])

        events = _run_stream(mock_llm, mock_sf=mock_sf)

        types = [e['type'] for e in events]
        assert 'content_filtered' in types
        done = events[-1]
        assert done['is_filtered'] is True
        assert done['filter_reason'] == 'output:尾词'

    def test_agent_ask_stream_when_final_flush_exception_then_swallows(self):
        """收尾 flush 抛异常：仅记录日志，流程正常收尾"""
        mock_sf = _default_sf()
        mock_sf.flush.side_effect = Exception('flush failed')

        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'delta': '内容'}, {'finish': True, 'latency_ms': 5},
        ])

        events = _run_stream(mock_llm, mock_sf=mock_sf)

        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['内容']
        assert events[-1]['type'] == 'done'
        assert events[-1]['is_filtered'] is False

    def test_agent_ask_stream_when_no_delta_then_emits_first_token(self):
        """全程无文本 delta（首帧即 finish）：补 first_token 事件再 done"""
        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([{'finish': True, 'latency_ms': 5}])

        events = _run_stream(mock_llm, mock_sf=_default_sf())

        types = [e['type'] for e in events]
        assert types == ['first_token', 'done']
        assert events[-1]['answer'] == ''

    def test_agent_ask_stream_when_error_round_then_ttfb_fallback(self):
        """异常轮 flush 无输出时，first_token 由收尾逻辑兜底补发"""
        mock_sf = _default_sf()
        mock_sf.flush.return_value = ([], None)

        mock_llm = MagicMock()
        mock_llm.stream.side_effect = Exception('boom')

        events = _run_stream(mock_llm, mock_sf=mock_sf)
        assert events[-1]['type'] == 'error'


class TestCustomToolRegistration:
    """自定义工具注册进真实 ToolRegistry 后参与 ReAct 循环"""

    def test_agent_ask_when_custom_tool_then_executed(self):
        """注册自定义工具：LLM 决策调用 → 参数解析 → 真实注册表执行"""
        from apps.agent.tools import ToolRegistry, BaseTool

        class UpperTool(BaseTool):
            """测试用自定义工具：文本转大写"""
            name = 'upper'
            description = '将文本转为大写'
            parameters = {
                'type': 'object',
                'properties': {'text': {'type': 'string'}},
                'required': ['text'],
            }

            def execute(self, ctx, text='', **kwargs):
                return {'result': text.upper(), 'ok': True, 'meta': {}}

        registry = ToolRegistry()
        registry.register(UpperTool())

        mock_llm = MagicMock()
        calls = {'n': 0}

        def chat_side_effect(msgs, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                return {'content': None, 'tool_calls': [
                    {'id': 'c1', 'name': 'upper', 'arguments': '{"text": "hello"}'}],
                    'latency_ms': 10}
            return {'content': '结果是 HELLO', 'latency_ms': 20}

        mock_llm.chat.side_effect = chat_side_effect
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        with patch.object(react, 'get_llm', return_value=mock_llm), \
                patch.object(react, 'get_default_registry', return_value=registry), \
                patch.object(react, 'MemoryManager', return_value=mock_mm), \
                patch.object(react, 'build_agent_messages', return_value=[]):
            result = react.agent_ask(None, '大写 hello', None,
                                     root_types=['company_doc'])

        assert result['answer'] == '结果是 HELLO'
        assert len(result['tool_traces']) == 1
        trace = result['tool_traces'][0]
        assert trace['tool_name'] == 'upper'
        assert trace['tool_args'] == {'text': 'hello'}
        assert trace['result'] == 'HELLO'
        assert trace['ok'] is True
        # 自定义工具 schema 通过 to_openai_tools 导出
        names = [t['function']['name'] for t in registry.to_openai_tools()]
        assert 'upper' in names
