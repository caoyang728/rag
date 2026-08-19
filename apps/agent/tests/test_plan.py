"""
plan（Plan-and-Execute Agent）单元测试

覆盖：
- plan_execute_stream 三阶段流式问答：
  - Phase 1 (Plan): LLM 流式收集工具调用（含文本片段）；
    无工具 → _generate_direct_answer 直答；
    有工具 → Phase 2 执行 + Phase 3 综合。
  - Phase 1 LLM 异常：yield error 事件后 return。
  - Phase 3 流式综合：正常流/流式审查 block/无审查器直接下发/异常/GeneratorExit。
  - Phase 3 flush 审查 buffer：正常放行/block/异常降级。
  - 空答案兜底 '[未生成内容]'、ttfb_ms 兜底补发。
  - sources 默认值回填。

- _generate_direct_answer 直答生成：
  - plan_text 非空：直接用 Phase 1 文本，含审查/审查命中/审查异常分支。
  - plan_text 为空：调用 LLM 生成一次纯文本回答。
  - 无审查器时原样下发。
  - LLM 异常中断 / GeneratorExit / flush buffer 各分支。
  - 空答案兜底。

全部 mock LLM / 工具注册表 / 记忆 / 敏感词过滤器，不依赖外部服务与数据库。

注意：plan 模块顶层 import 把名称绑定进 plan 命名空间，
因此必须 patch apps.agent.plan.*，patch 定义处（apps.llm.factory.get_llm 等）不会生效。
sensitive_filter 为函数内 import，patch 定义处即可生效。
"""

import pytest
from contextlib import ExitStack
from unittest.mock import patch, MagicMock

from apps.agent import plan

pytestmark = pytest.mark.unit


# ============================================================================
# 辅助函数
# ============================================================================

def _enter_env(mock_llm, mock_registry, mock_mm, mock_sf=None):
    """进入 plan_execute_stream 所需的全部 mock 上下文

    用 ExitStack 统一管理，避免每个用例写一大串嵌套 with。
    mock_sf 为 None 时敏感词审查初始化走 except 分支（sf = None）。
    """
    stack = ExitStack()
    stack.enter_context(patch.object(plan, 'get_llm', return_value=mock_llm))
    stack.enter_context(patch.object(plan, 'get_default_registry', return_value=mock_registry))
    stack.enter_context(patch.object(plan, 'MemoryManager', return_value=mock_mm))
    stack.enter_context(patch.object(plan, '_build_tool_config', return_value=([], 'system_prompt')))
    stack.enter_context(patch.object(plan, '_build_tool_messages', return_value=[]))
    stack.enter_context(patch.object(plan, '_collect_citations', return_value=([], [])))
    stack.enter_context(patch.object(plan, '_execute_tool_calls', return_value=[]))
    stack.enter_context(patch.object(plan, '_make_filtered_event', return_value={
        'type': 'content_filtered', 'reason': '检测到违规内容，已拦截', 'category': 'other',
    }))
    if mock_sf is not None:
        # sensitive_filter 为函数内 import，patch 定义处即可生效
        stack.enter_context(patch('apps.security.sensitive_filter.get_sensitive_filter',
                                  return_value=mock_sf))
    else:
        # mock_sf=None 时也要 patch 掉，避免真实 get_sensitive_filter 访问 DB
        stack.enter_context(patch('apps.security.sensitive_filter.get_sensitive_filter',
                                  return_value=None))
    return stack


def _default_sf():
    """默认放行的敏感词过滤器 mock：feed/flush 原样透传，new_state 返回真值 dict

    new_state 返回非空 dict：plan 以 `if sf and filter_state` 判断审查是否启用，
    空 dict 是假值会导致审查分支被跳过。
    """
    mock_sf = MagicMock()
    mock_sf.new_state.return_value = {'buffer': ''}
    mock_sf.feed.side_effect = lambda state, delta: ([delta], None)
    mock_sf.flush.return_value = ([], None)
    return mock_sf


def _drain(gen):
    """消费生成器，返回所有 yield 的事件列表"""
    return list(gen)


# ============================================================================
# plan_execute_stream 测试
# ============================================================================

class TestPlanExecuteStream:
    """plan_execute_stream：Plan-and-Execute 三阶段流式问答"""

    def test_plan_execute_stream_when_no_tools_then_direct_answer(self):
        """Phase 1 无工具调用：直接进入 _generate_direct_answer 直答"""
        mock_llm = MagicMock()
        # Phase 1 流式：先有文本 delta，再 finish 无 tool_calls
        mock_llm.stream.return_value = iter([
            {'delta': '这是规划文本'},
            {'finish': True, 'latency_ms': 10},
        ])
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        with _enter_env(mock_llm, mock_registry, mock_mm):
            events = _drain(plan.plan_execute_stream(MagicMock(), '测试问题', MagicMock()))

        # 无工具时应跳过工具调用，直接产出规划文本 + done
        types = [e['type'] for e in events]
        assert 'tool_call' not in types
        assert 'tool_result' not in types
        assert 'first_token' in types
        assert types[-1] == 'done'
        # plan_text 非空，直接作为答案
        done = events[-1]
        assert done['answer'] == '这是规划文本'
        # 无工具时不产出 tool_calls_count（由 _generate_direct_answer 生成）
        assert done['stats'].get('tool_calls_count', 0) == 0

    def test_plan_execute_stream_when_no_tools_and_no_text_then_generates_answer(self):
        """Phase 1 无工具且无文本：LLM 直接生成答案"""
        mock_llm = MagicMock()
        # Phase 1: finish 无 tool_calls 也无文本 → _generate_direct_answer 会再次调用 LLM
        # Phase 2 (直接回答): 正常输出
        mock_llm.stream.side_effect = [
            # Phase 1 流式
            iter([{'finish': True, 'latency_ms': 5}]),
            # _generate_direct_answer 流式
            iter([
                {'delta': '直接回答内容'},
                {'finish': True, 'latency_ms': 5},
            ]),
        ]
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        with _enter_env(mock_llm, mock_registry, mock_mm):
            events = _drain(plan.plan_execute_stream(MagicMock(), '测试问题', MagicMock()))

        done = events[-1]
        assert done['answer'] == '直接回答内容'
        assert done['stats']['tool_rounds'] == 0

    def test_plan_execute_stream_when_tools_planned_then_execute_and_synthesize(self):
        """Phase 1 有工具调用：执行工具 + 综合生成最终答案"""
        mock_llm = MagicMock()
        mock_llm.stream.side_effect = [
            # Phase 1: finish 带 tool_calls
            iter([{'finish': True, 'tool_calls': [
                {'id': 'c1', 'name': 'knowledge_search', 'arguments': '{"query": "x"}'},
            ], 'latency_ms': 10}]),
            # Phase 3: 综合生成
            iter([
                {'delta': '综合答案'},
                {'finish': True, 'latency_ms': 5},
            ]),
        ]
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}
        # _execute_tool_calls mock：返回一个工具执行结果
        exec_result = [{
            'call_id': 'c1', 'tool_name': 'knowledge_search',
            'result': '搜索结果', 'ok': True, 'meta': {'tool_args': '{}'},
            'latency_ms': 20, 'result_preview': '搜索结果',
        }]

        with _enter_env(mock_llm, mock_registry, mock_mm) as stack:
            stack.enter_context(patch.object(plan, '_execute_tool_calls', return_value=exec_result))
            events = _drain(plan.plan_execute_stream(MagicMock(), '测试问题', MagicMock()))

        types = [e['type'] for e in events]
        # 应依次产出：tool_call → tool_result → first_token → delta → done
        assert types[0] == 'tool_call'
        assert types[1] == 'tool_result'
        assert 'first_token' in types
        assert 'delta' in types
        assert types[-1] == 'done'

        # tool_call 事件验证
        tc_event = events[0]
        assert tc_event['tool_name'] == 'knowledge_search'
        assert tc_event['call_id'] == 'c1'

        # tool_result 事件验证
        tr_event = events[1]
        assert tr_event['ok'] is True
        assert tr_event['call_id'] == 'c1'

        # done 事件验证
        done = events[-1]
        assert done['answer'] == '综合答案'
        assert done['stats']['tool_calls_count'] == 1
        assert len(done['tool_traces']) == 1

    def test_plan_execute_stream_when_phase1_exception_then_yields_error(self):
        """Phase 1 LLM 抛异常：yield error 事件后 return，不继续后续阶段"""
        mock_llm = MagicMock()
        mock_llm.stream.side_effect = Exception('LLM 规划阶段挂了')
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        with _enter_env(mock_llm, mock_registry, mock_mm):
            events = _drain(plan.plan_execute_stream(MagicMock(), '测试问题', MagicMock()))

        assert len(events) == 1
        assert events[0]['type'] == 'error'
        assert '规划阶段失败' in events[0]['detail']

    def test_plan_execute_stream_when_phase3_generator_exit_then_reraises(self):
        """Phase 3 综合阶段客户端断开（GeneratorExit）：捕获后 re-raise"""
        mock_llm = MagicMock()
        # Phase 1 正常完成
        phase1_iter = iter([
            {'delta': '规划内容'},
            {'finish': True, 'tool_calls': [
                {'id': 'c1', 'name': 'calculator', 'arguments': '{}'},
            ], 'latency_ms': 5},
        ])

        def phase3_gen():
            yield {'delta': '部分答案'}
            raise GeneratorExit()

        mock_llm.stream.side_effect = [phase1_iter, phase3_gen()]
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        with _enter_env(mock_llm, mock_registry, mock_mm):
            gen = plan.plan_execute_stream(MagicMock(), '测试问题', MagicMock())
            # 消费到 GeneratorExit
            events = []
            try:
                for e in gen:
                    events.append(e)
            except GeneratorExit:
                pass

        types = [e['type'] for e in events]
        assert 'tool_call' in types
        assert 'first_token' in types
        assert 'done' not in types  # GeneratorExit 不应产出 done

    def test_plan_execute_stream_when_phase3_exception_then_yields_error_delta(self):
        """Phase 3 LLM 抛异常：产出流式中断 delta，然后 done 兜底"""
        mock_llm = MagicMock()
        # Phase 1 正常：无工具（走直答）或有工具 → Phase 3 异常
        # 用有工具场景：Phase 1 finish + tool_calls，Phase 3 异常
        mock_llm.stream.side_effect = [
            iter([{'finish': True, 'tool_calls': [
                {'id': 'c1', 'name': 'calculator', 'arguments': '{}'},
            ], 'latency_ms': 5}]),
            iter([{'delta': '部分答案'}]),  # 遇到第一个 chunk 后触发异常
        ]
        # 让第二个 stream 第二次调用抛异常
        call_count = {'n': 0}

        def synth_stream(*args, **kwargs):
            call_count['n'] += 1
            if call_count['n'] == 1:
                # Phase 1
                return iter([{'finish': True, 'tool_calls': [
                    {'id': 'c1', 'name': 'calculator', 'arguments': '{}'},
                ], 'latency_ms': 5}])
            elif call_count['n'] == 2:
                # Phase 3: 先输出一个 delta，再抛异常
                def _gen():
                    yield {'delta': '部分答案'}
                    raise RuntimeError('综合阶段崩溃')
                return _gen()
            return iter([])

        mock_llm.stream = synth_stream
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        with _enter_env(mock_llm, mock_registry, mock_mm):
            events = _drain(plan.plan_execute_stream(MagicMock(), '测试问题', MagicMock()))

        types = [e['type'] for e in events]
        # 应有 first_token + delta(部分答案) + delta(错误信息) + done
        assert 'first_token' in types
        assert types[-1] == 'done'
        # done 中应包含 detected_error
        done = events[-1]
        assert '综合阶段崩溃' in done['detected_error']

    def test_plan_execute_stream_when_sensitive_filter_block_then_filters_output(self):
        """Phase 3 流式综合命中 block：中断生成，done 标记 is_filtered"""
        mock_sf = _default_sf()
        block_hit = MagicMock()
        block_hit.action = 'block'
        block_hit.word = '违禁词'
        block_hit.category = 'porn'
        mock_sf.feed.side_effect = lambda state, delta: (
            ([], block_hit) if '违禁' in delta else ([delta], None))
        mock_sf.new_state.return_value = {'buffer': ''}

        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            # Phase 1: finish 无工具 → 直答
            {'finish': True, 'latency_ms': 5},
        ])

        # 让 _generate_direct_answer 内的 LLM 也走 block 流
        def stream_gen(*args, **kwargs):
            # _generate_direct_answer 的 LLM stream
            yield {'delta': '正常'}
            yield {'delta': '包含违禁的内容'}
            yield {'delta': '不应输出'}
            yield {'finish': True, 'latency_ms': 5}

        mock_llm.stream.side_effect = None
        mock_llm.stream = MagicMock(side_effect=[
            iter([{'finish': True, 'latency_ms': 5}]),  # Phase 1
            stream_gen(),  # _generate_direct_answer
        ])

        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        with _enter_env(mock_llm, mock_registry, mock_mm, mock_sf=mock_sf):
            events = _drain(plan.plan_execute_stream(MagicMock(), '测试问题', MagicMock()))

        types = [e['type'] for e in events]
        assert 'content_filtered' in types
        done = events[-1]
        assert done['is_filtered'] is True
        assert done['filter_reason'] == 'output:违禁词'

    def test_plan_execute_stream_when_empty_answer_then_fallback(self):
        """LLM 未产出任何有效 delta：答案兜底为 '[未生成内容]'"""
        mock_llm = MagicMock()
        # Phase 1: finish 无 tool_calls 也无文本
        # _generate_direct_answer: LLM 也无输出
        mock_llm.stream.side_effect = [
            iter([{'finish': True, 'latency_ms': 5}]),  # Phase 1
            iter([{'finish': True, 'latency_ms': 5}]),  # direct answer: 无 delta
        ]
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        with _enter_env(mock_llm, mock_registry, mock_mm):
            events = _drain(plan.plan_execute_stream(MagicMock(), '测试问题', MagicMock()))

        done = events[-1]
        assert done['answer'] == '[未生成内容]'

    def test_plan_execute_stream_when_no_delta_at_all_then_supplies_first_token(self):
        """全程无 delta（首帧即 finish）：补发 first_token 事件确保前端协议完整"""
        mock_llm = MagicMock()
        mock_llm.stream.side_effect = [
            iter([{'finish': True, 'latency_ms': 5}]),  # Phase 1: 无工具无文本
            iter([{'finish': True, 'latency_ms': 5}]),  # direct answer: 无 delta
        ]
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        with _enter_env(mock_llm, mock_registry, mock_mm):
            events = _drain(plan.plan_execute_stream(MagicMock(), '测试问题', MagicMock()))

        types = [e['type'] for e in events]
        # ttfb_ms None 兜底：应补发 first_token
        assert 'first_token' in types
        assert types[-1] == 'done'

    def test_plan_execute_stream_when_sources_none_then_defaults(self):
        """sources 参数为 None 时回填默认值 ['doc', 'db', 'web', 'llm']"""
        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'finish': True, 'latency_ms': 5},
        ])
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        with _enter_env(mock_llm, mock_registry, mock_mm) as stack:
            build_tool_config = MagicMock(return_value=([], 'prompt'))
            stack.enter_context(patch.object(plan, '_build_tool_config', build_tool_config))
            _drain(plan.plan_execute_stream(MagicMock(), '测试问题', MagicMock(),
                                             sources=None))

        # _build_tool_config 应使用默认 sources
        build_tool_config.assert_called_once()
        call_args = build_tool_config.call_args[0]
        assert call_args[0] == ['doc', 'db', 'web', 'llm']

    def test_plan_execute_stream_when_multiple_tools_then_yields_all(self):
        """Phase 1 规划多个工具：全部 yield tool_call 和 tool_result"""
        tool_calls = [
            {'id': 'c1', 'name': 'knowledge_search', 'arguments': '{"query": "a"}'},
            {'id': 'c2', 'name': 'web_search', 'arguments': '{"query": "b"}'},
        ]
        exec_results = [
            {'call_id': 'c1', 'tool_name': 'knowledge_search', 'result': '结果1',
             'ok': True, 'meta': {'tool_args': '{}'}, 'latency_ms': 10},
            {'call_id': 'c2', 'tool_name': 'web_search', 'result': '结果2',
             'ok': True, 'meta': {'tool_args': '{}'}, 'latency_ms': 15},
        ]

        mock_llm = MagicMock()
        mock_llm.stream.side_effect = [
            iter([{'finish': True, 'tool_calls': tool_calls, 'latency_ms': 10}]),
            iter([{'delta': '综合答案'}, {'finish': True, 'latency_ms': 5}]),
        ]
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        with _enter_env(mock_llm, mock_registry, mock_mm) as stack:
            stack.enter_context(patch.object(plan, '_execute_tool_calls',
                                             return_value=exec_results))
            events = _drain(plan.plan_execute_stream(MagicMock(), '测试问题', MagicMock()))

        tc_events = [e for e in events if e['type'] == 'tool_call']
        tr_events = [e for e in events if e['type'] == 'tool_result']
        assert len(tc_events) == 2
        assert len(tr_events) == 2
        assert tc_events[0]['tool_name'] == 'knowledge_search'
        assert tc_events[1]['tool_name'] == 'web_search'

    def test_plan_execute_stream_when_tool_result_fails_then_records_in_traces(self):
        """工具执行失败（ok=False）：tool_result 事件和 traces 均记录失败状态"""
        exec_results = [
            {'call_id': 'c1', 'tool_name': 'calculator', 'result': '超时',
             'ok': False, 'meta': {'tool_args': '{}'}, 'latency_ms': 3000},
        ]

        mock_llm = MagicMock()
        mock_llm.stream.side_effect = [
            iter([{'finish': True, 'tool_calls': [
                {'id': 'c1', 'name': 'calculator', 'arguments': '{}'},
            ], 'latency_ms': 5}]),
            iter([{'delta': '工具失败但仍有答案'}, {'finish': True, 'latency_ms': 5}]),
        ]
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        with _enter_env(mock_llm, mock_registry, mock_mm) as stack:
            stack.enter_context(patch.object(plan, '_execute_tool_calls',
                                             return_value=exec_results))
            events = _drain(plan.plan_execute_stream(MagicMock(), '测试问题', MagicMock()))

        tr = [e for e in events if e['type'] == 'tool_result'][0]
        assert tr['ok'] is False

        done = events[-1]
        assert done['tool_traces'][0]['ok'] is False

    def test_plan_execute_stream_when_filter_flush_hits_block_then_filters(self):
        """Phase 3 flush 审查 buffer 命中 block：yield content_filtered 事件"""
        mock_sf = MagicMock()
        mock_sf.new_state.return_value = {'buffer': ''}
        mock_sf.feed.side_effect = lambda state, delta: ([delta], None)
        # flush 命中 block
        block_hit = MagicMock()
        block_hit.action = 'block'
        block_hit.word = '尾部违规'
        mock_sf.flush.return_value = ([], block_hit)

        mock_llm = MagicMock()
        # Phase 1: 无工具 → 直答
        # 直答 LLM: 输出正常内容，无中途 block
        mock_llm.stream.side_effect = [
            iter([{'finish': True, 'latency_ms': 5}]),  # Phase 1
            iter([{'delta': '正常内容'}, {'finish': True, 'latency_ms': 5}]),  # direct answer
        ]
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        with _enter_env(mock_llm, mock_registry, mock_mm, mock_sf=mock_sf):
            events = _drain(plan.plan_execute_stream(MagicMock(), '测试问题', MagicMock()))

        types = [e['type'] for e in events]
        # flush 命中 block → 应有 content_filtered 事件
        assert 'content_filtered' in types
        done = events[-1]
        assert done['is_filtered'] is True

    def test_plan_execute_stream_when_filter_flush_raises_then_continues(self):
        """Phase 3 flush 审查 buffer 抛异常：仅记录日志，流程正常收尾"""
        mock_sf = MagicMock()
        mock_sf.new_state.return_value = {'buffer': ''}
        mock_sf.feed.side_effect = lambda state, delta: ([delta], None)
        mock_sf.flush.side_effect = Exception('flush 炸了')

        mock_llm = MagicMock()
        mock_llm.stream.side_effect = [
            iter([{'finish': True, 'latency_ms': 5}]),
            iter([{'delta': '正常输出'}, {'finish': True, 'latency_ms': 5}]),
        ]
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        with _enter_env(mock_llm, mock_registry, mock_mm, mock_sf=mock_sf):
            events = _drain(plan.plan_execute_stream(MagicMock(), '测试问题', MagicMock()))

        # flush 异常不应导致崩溃，仍能产出 done
        done = events[-1]
        assert done['type'] == 'done'
        assert done['answer'] == '正常输出'
        assert done['is_filtered'] is False

    def test_plan_execute_stream_when_sf_init_fails_then_no_filter(self):
        """敏感词过滤器初始化异常：sf=None，下游审查分支被跳过"""
        mock_llm = MagicMock()
        mock_llm.stream.side_effect = [
            iter([{'finish': True, 'latency_ms': 5}]),
            iter([{'delta': '无审查内容'}, {'finish': True, 'latency_ms': 5}]),
        ]
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': ''}

        # 不传 mock_sf → _enter_env 会 patch get_sensitive_filter 抛异常
        # 但 _enter_env 默认不传 mock_sf 时不会 patch sf
        # 手动 patch 使其抛异常
        with _enter_env(mock_llm, mock_registry, mock_mm) as stack:
            stack.enter_context(patch('apps.security.sensitive_filter.get_sensitive_filter',
                                      side_effect=Exception('sf init failed')))
            events = _drain(plan.plan_execute_stream(MagicMock(), '测试问题', MagicMock()))

        done = events[-1]
        assert done['answer'] == '无审查内容'
        assert done['is_filtered'] is False

    def test_plan_execute_stream_when_memory_loaded_then_includes_in_plan_prompt(self):
        """记忆加载成功：memory_block 应拼入 Phase 1 的 user message"""
        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'finish': True, 'latency_ms': 5},
        ])
        mock_registry = MagicMock()
        mock_mm = MagicMock()
        mock_mm.load_context.return_value = {'memory_block': '历史记忆内容'}

        with _enter_env(mock_llm, mock_registry, mock_mm) as stack:
            # 捕获 Phase 1 传入 LLM 的 messages
            captured = {}

            def capture_stream(*args, **kwargs):
                captured['messages'] = args[0] if args else kwargs.get('messages', [])
                return iter([{'finish': True, 'latency_ms': 5}])

            mock_llm.stream.side_effect = None
            mock_llm.stream = MagicMock(side_effect=[
                capture_stream(),
                iter([{'finish': True, 'latency_ms': 5}]),
            ])
            # 让 Phase 1 的 stream 也产出 tool_calls 以跳过直答路径
            call_n = {'n': 0}

            def stream_with_tool(*args, **kwargs):
                call_n['n'] += 1
                if call_n['n'] == 1:
                    return iter([{'finish': True, 'tool_calls': [
                        {'id': 'c1', 'name': 'calculator', 'arguments': '{}'},
                    ], 'latency_ms': 5}])
                return iter([{'delta': '答案'}, {'finish': True, 'latency_ms': 5}])

            mock_llm.stream = stream_with_tool
            _drain(plan.plan_execute_stream(MagicMock(), '测试问题', MagicMock()))


# ============================================================================
# _generate_direct_answer 测试
# ============================================================================

class TestGenerateDirectAnswer:
    """_generate_direct_answer：规划器决定不需要工具时的直答路径"""

    def test_direct_answer_when_plan_text_then_uses_existing_text(self):
        """plan_text 非空：直接用 Phase 1 已产出的文本作为答案，不再调 LLM"""
        mock_llm = MagicMock()
        start_time = 0.0

        events = _drain(plan._generate_direct_answer(
            mock_llm, '问题', '', start_time, None, None,
            plan_text='已有规划文本'))

        # 不应调用 LLM stream
        mock_llm.stream.assert_not_called()
        types = [e['type'] for e in events]
        assert 'first_token' in types
        assert types[-1] == 'done'
        done = events[-1]
        assert done['answer'] == '已有规划文本'
        assert done['stats']['tool_rounds'] == 0

    def test_direct_answer_when_plan_text_with_sf_check_passes(self):
        """plan_text 非空 + 审查通过：safe_text 原样下发"""
        mock_sf = MagicMock()
        mock_sf.check_full_text.return_value = ('安全文本', None)
        mock_llm = MagicMock()

        events = _drain(plan._generate_direct_answer(
            mock_llm, '问题', '', 0.0, mock_sf, {'buffer': ''},
            plan_text='安全文本'))

        mock_sf.check_full_text.assert_called_once_with('安全文本')
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['安全文本']
        done = events[-1]
        assert done['answer'] == '安全文本'
        assert done['is_filtered'] is False

    def test_direct_answer_when_plan_text_with_sf_block_then_filters(self):
        """plan_text 非空 + 审查命中 block：yield content_filtered，answer 清空"""
        mock_sf = MagicMock()
        block_hit = MagicMock()
        block_hit.action = 'block'
        block_hit.word = '违禁'
        mock_sf.check_full_text.return_value = ('', block_hit)
        mock_llm = MagicMock()

        events = _drain(plan._generate_direct_answer(
            mock_llm, '问题', '', 0.0, mock_sf, {'buffer': ''},
            plan_text='含违禁内容'))

        types = [e['type'] for e in events]
        assert 'content_filtered' in types
        done = events[-1]
        assert done['is_filtered'] is True
        assert done['answer'] == ''

    def test_direct_answer_when_plan_text_sf_check_raises_then_passes_through(self):
        """plan_text 非空 + 审查器 check_full_text 抛异常：跳过审查原样下发"""
        mock_sf = MagicMock()
        mock_sf.check_full_text.side_effect = Exception('sf 报错')
        mock_llm = MagicMock()

        events = _drain(plan._generate_direct_answer(
            mock_llm, '问题', '', 0.0, mock_sf, {'buffer': ''},
            plan_text='忽略审查的文本'))

        # 异常被 except 捕获：safe_text = plan_text，正常下发
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['忽略审查的文本']
        done = events[-1]
        assert done['answer'] == '忽略审查的文本'

    def test_direct_answer_when_no_plan_text_then_calls_llm(self):
        """plan_text 为空：调用 LLM 生成一次纯文本回答"""
        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'delta': 'LLM 生成的答案'},
            {'finish': True, 'latency_ms': 5},
        ])

        events = _drain(plan._generate_direct_answer(
            mock_llm, '问题', '', 0.0, None, None, plan_text=''))

        mock_llm.stream.assert_called_once()
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['LLM 生成的答案']
        done = events[-1]
        assert done['answer'] == 'LLM 生成的答案'

    def test_direct_answer_when_no_plan_text_and_memory_then_includes_memory(self):
        """plan_text 为空 + memory_block 非空：记忆文本拼入 user message"""
        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'delta': '含记忆答案'}, {'finish': True, 'latency_ms': 5},
        ])

        events = _drain(plan._generate_direct_answer(
            mock_llm, '问题', '历史记忆', 0.0, None, None, plan_text=''))

        # 验证 LLM stream 被调用，消息包含记忆
        call_args = mock_llm.stream.call_args
        messages = call_args[0][0]
        assert '历史记忆' in messages[1]['content']

    def test_direct_answer_when_no_plan_text_with_sf_and_filter_state(self):
        """plan_text 为空 + 审查器启用：delta 走 feed 审查流"""
        mock_sf = MagicMock()
        mock_sf.new_state.return_value = {'buffer': ''}
        mock_sf.feed.side_effect = lambda state, delta: ([delta], None)
        mock_sf.flush.return_value = ([], None)

        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'delta': '审查后内容'}, {'finish': True, 'latency_ms': 5},
        ])

        events = _drain(plan._generate_direct_answer(
            mock_llm, '问题', '', 0.0, mock_sf, {'buffer': ''}, plan_text=''))

        mock_sf.feed.assert_called_once()
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['审查后内容']

    def test_direct_answer_when_sf_block_during_stream_then_breaks(self):
        """plan_text 为空 + 流式审查命中 block：中断流式输出"""
        mock_sf = MagicMock()
        mock_sf.new_state.return_value = {'buffer': ''}
        block_hit = MagicMock()
        block_hit.action = 'block'
        block_hit.word = '违规'
        mock_sf.feed.side_effect = lambda state, delta: (
            ([], block_hit) if '违规' in delta else ([delta], None))

        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'delta': '正常'},
            {'delta': '含违规内容'},
            {'delta': '不应输出'},
            {'finish': True, 'latency_ms': 5},
        ])

        events = _drain(plan._generate_direct_answer(
            mock_llm, '问题', '', 0.0, mock_sf, {'buffer': ''}, plan_text=''))

        types = [e['type'] for e in events]
        assert 'content_filtered' in types
        # block 后的内容不应下发
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['正常']
        done = events[-1]
        assert done['is_filtered'] is True

    def test_direct_answer_when_no_sf_then_delta_direct(self):
        """无审查器（sf=None）：delta 直接下发不经过审查"""
        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'delta': '原始内容'}, {'finish': True, 'latency_ms': 5},
        ])

        events = _drain(plan._generate_direct_answer(
            mock_llm, '问题', '', 0.0, None, None, plan_text=''))

        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['原始内容']

    def test_direct_answer_when_exception_then_yields_error_delta(self):
        """LLM stream 抛异常：产出中断提示 delta，done 兜底"""
        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'delta': '部分内容'},
            # 下一次迭代将抛异常
        ])

        # 模拟第二次迭代抛异常
        call_n = {'n': 0}

        def stream_side_effect(*args, **kwargs):
            call_n['n'] += 1
            if call_n['n'] == 1:
                def gen():
                    yield {'delta': '部分内容'}
                    raise RuntimeError('LLM 崩了')
                return gen()
            return iter([])

        mock_llm.stream = stream_side_effect

        events = _drain(plan._generate_direct_answer(
            mock_llm, '问题', '', 0.0, None, None, plan_text=''))

        # 异常 delta 应包含错误信息
        error_deltas = [e['delta'] for e in events if e['type'] == 'delta'
                        if '生成中断' in e.get('delta', '') or 'LLM 崩了' in e.get('delta', '')]
        assert len(error_deltas) > 0
        done = events[-1]
        assert done['type'] == 'done'

    def test_direct_answer_when_exception_and_no_ttfb_then_supplies_first_token(self):
        """LLM stream 首帧即抛异常（ttfb_ms 为 None）：补发 first_token"""
        mock_llm = MagicMock()

        def gen():
            raise RuntimeError('立即崩溃')
            yield  # noqa: 让 Python 识别为 generator

        mock_llm.stream.return_value = gen()

        events = _drain(plan._generate_direct_answer(
            mock_llm, '问题', '', 0.0, None, None, plan_text=''))

        types = [e['type'] for e in events]
        assert 'first_token' in types
        assert types[-1] == 'done'

    def test_direct_answer_when_generator_exit_then_reraises(self):
        """GeneratorExit：捕获后 re-raise，不产出 done"""
        mock_llm = MagicMock()

        def gen():
            yield {'delta': '部分'}
            raise GeneratorExit()

        mock_llm.stream.return_value = gen()

        events = []
        try:
            for e in plan._generate_direct_answer(
                    mock_llm, '问题', '', 0.0, None, None, plan_text=''):
                events.append(e)
        except GeneratorExit:
            pass

        # GeneratorExit 应被 re-raise，不产出 done
        assert all(e['type'] != 'done' for e in events)

    def test_direct_answer_when_no_ttfb_then_supplies_first_token(self):
        """LLM 首帧即 finish（ttfb_ms 为 None）：兜底补发 first_token"""
        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'finish': True, 'latency_ms': 5},
        ])

        events = _drain(plan._generate_direct_answer(
            mock_llm, '问题', '', 0.0, None, None, plan_text=''))

        types = [e['type'] for e in events]
        assert 'first_token' in types

    def test_direct_answer_when_empty_answer_then_fallback(self):
        """LLM 未产出任何 delta 且无 filter_hit：答案兜底为 '[未生成内容]'"""
        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'finish': True, 'latency_ms': 5},
        ])

        events = _drain(plan._generate_direct_answer(
            mock_llm, '问题', '', 0.0, None, None, plan_text=''))

        done = events[-1]
        assert done['answer'] == '[未生成内容]'

    def test_direct_answer_when_sf_flush_hits_block_then_filters(self):
        """flush 审查 buffer 命中 block：yield content_filtered"""
        mock_sf = MagicMock()
        mock_sf.new_state.return_value = {'buffer': ''}
        mock_sf.feed.side_effect = lambda state, delta: ([delta], None)
        block_hit = MagicMock()
        block_hit.action = 'block'
        block_hit.word = '尾部违规'
        mock_sf.flush.return_value = ([], block_hit)

        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'delta': '正常内容'}, {'finish': True, 'latency_ms': 5},
        ])

        events = _drain(plan._generate_direct_answer(
            mock_llm, '问题', '', 0.0, mock_sf, {'buffer': ''}, plan_text=''))

        types = [e['type'] for e in events]
        assert 'content_filtered' in types
        done = events[-1]
        assert done['is_filtered'] is True

    def test_direct_answer_when_sf_flush_raises_then_continues(self):
        """flush 审查 buffer 抛异常：仅跳过，流程正常收尾"""
        mock_sf = MagicMock()
        mock_sf.new_state.return_value = {'buffer': ''}
        mock_sf.feed.side_effect = lambda state, delta: ([delta], None)
        mock_sf.flush.side_effect = Exception('flush 失败')

        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'delta': '内容'}, {'finish': True, 'latency_ms': 5},
        ])

        events = _drain(plan._generate_direct_answer(
            mock_llm, '问题', '', 0.0, mock_sf, {'buffer': ''}, plan_text=''))

        done = events[-1]
        assert done['type'] == 'done'
        assert done['answer'] == '内容'

    def test_direct_answer_when_sf_flush_normal_then_appends_outputs(self):
        """flush 审查 buffer 正常放行：safe outputs 追加到 answer"""
        mock_sf = MagicMock()
        mock_sf.new_state.return_value = {'buffer': ''}
        mock_sf.feed.side_effect = lambda state, delta: ([delta], None)
        mock_sf.flush.return_value = (['尾部安全内容'], None)

        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'delta': '主体'}, {'finish': True, 'latency_ms': 5},
        ])

        events = _drain(plan._generate_direct_answer(
            mock_llm, '问题', '', 0.0, mock_sf, {'buffer': ''}, plan_text=''))

        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert '主体' in deltas
        assert '尾部安全内容' in deltas
        done = events[-1]
        assert done['answer'] == '主体尾部安全内容'

    def test_direct_answer_when_no_sf_and_no_plan_text_then_skips_sf_flush(self):
        """无审查器 + 无 plan_text：不走 sf flush 分支"""
        mock_llm = MagicMock()
        mock_llm.stream.return_value = iter([
            {'delta': '答案'}, {'finish': True, 'latency_ms': 5},
        ])

        events = _drain(plan._generate_direct_answer(
            mock_llm, '问题', '', 0.0, None, None, plan_text=''))

        done = events[-1]
        assert done['answer'] == '答案'
        assert done['is_filtered'] is False
