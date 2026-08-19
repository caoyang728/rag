"""
Plan-and-Execute Agent
先规划再并行执行，适合需要多工具组合的复杂问题。
与 ReAct 的区别：不做多轮循环，而是规划一次 → 并行执行 → 综合生成。

三阶段流水线：
  Phase 1 (Plan): LLM 分析问题，输出所有需要的工具调用（1 次 LLM）
  Phase 2 (Execute): 并行执行所有工具调用
  Phase 3 (Synthesize): LLM 综合所有工具结果生成最终答案（1 次 LLM）

总 LLM 调用 2 次（规划 + 综合），比 ReAct 的 3-5 次更少。
"""

import time
from typing import Any, Dict, Iterator, List

from loguru import logger

from apps.llm.factory import get_llm
from apps.llm.prompts.agent import PLANNER_SYSTEM_PROMPT, SYNTHESIZER_SYSTEM_PROMPT
from apps.memory.manager import MemoryManager

from .tools import ToolContext, get_default_registry, parse_tool_arguments
from .react import (
    _build_tool_config, _build_tool_messages, _collect_citations,
    _execute_tool_calls, _make_filtered_event,
)


def plan_execute_stream(user, question: str, session,
                        root_types: list = None, node_ids: list = None,
                        sources: list = None) -> Iterator[Dict[str, Any]]:
    """Plan-and-Execute 流式问答

    yield SSE 事件：
    - tool_call / tool_result：工具调用过程（前端渲染"思考过程"区）
    - first_token / delta：最终答案的流式文本
    - done：结束（含 citations / tool_traces / stats）

    Args:
        user: 当前用户对象
        question: 用户问题
        session: 会话对象
        root_types: 知识库根类型列表
        node_ids: 知识范围节点 ID 列表
        sources: 数据来源开关列表（doc/db/web/llm）

    Yields:
        SSE 事件 dict
    """
    if sources is None:
        sources = ['doc', 'db', 'web', 'llm']

    start_time = time.time()
    tool_registry = get_default_registry()
    ctx = ToolContext(user=user, root_types=root_types, node_ids=node_ids, session=session)

    # 记忆加载
    mm = MemoryManager()
    root_type = (root_types or ['company_doc'])[0]
    mem_ctx = mm.load_context(user, session, question, root_type=root_type)
    memory_block = mem_ctx.get('memory_block', '')

    # 构建工具配置（按 sources 过滤可用工具）
    openai_tools, system_prompt = _build_tool_config(sources, tool_registry)

    # 内容审查初始化
    sf = None
    filter_state = None
    try:
        from apps.security.sensitive_filter import get_sensitive_filter
        sf = get_sensitive_filter()
        filter_state = sf.new_state()
    except Exception:
        pass

    # Start 事件（由调用方 _ask_stream_via_plan 统一发送，这里不重复）

    llm = get_llm()

    # ========================================================================
    # Phase 1: Plan - LLM 规划所有需要的工具调用
    # ========================================================================
    plan_start = time.time()
    plan_messages = [
        {'role': 'system', 'content': PLANNER_SYSTEM_PROMPT},
        {'role': 'user', 'content': (memory_block + '\n\n' if memory_block else '') + '## 用户问题\n' + question},
    ]

    tool_calls_collected: List[Dict] = []
    plan_text_parts: List[str] = []

    try:
        for chunk in llm.stream(plan_messages, tools=openai_tools,
                                tool_choice='auto', temperature=0.3, max_tokens=2048):
            if chunk.get('finish'):
                tool_calls_collected = chunk.get('tool_calls') or []
                break
            delta = chunk.get('delta', '')
            if delta:
                plan_text_parts.append(delta)
    except Exception as e:
        logger.exception('[Plan] Phase 1 - Planner LLM failed')
        yield {'type': 'error', 'detail': f'规划阶段失败: {e}'}
        return

    plan_latency = int((time.time() - plan_start) * 1000)

    # 解析工具参数
    for tc in tool_calls_collected:
        tc['arguments'] = parse_tool_arguments(tc.get('arguments', ''))

    # 规划器决定不需要工具：直接生成答案（plan_text_parts 可能有内容，也可能为空）
    if not tool_calls_collected:
        logger.info('[Plan] No tools planned, generating direct answer')
        yield from _generate_direct_answer(
            llm, question, memory_block, start_time, sf, filter_state,
            plan_text=''.join(plan_text_parts),
        )
        return

    logger.info(f'[Plan] Phase 1 done: {len(tool_calls_collected)} tools planned ({plan_latency}ms)')

    # Yield tool_call events（前端渲染思考过程区）
    for tc in tool_calls_collected:
        yield {
            'type': 'tool_call',
            'call_id': tc.get('id', ''),
            'tool_name': tc.get('name', ''),
            'tool_args': tc.get('arguments', ''),
        }

    # ========================================================================
    # Phase 2: Execute - 并行执行所有工具调用
    # ========================================================================
    exec_start = time.time()
    results = _execute_tool_calls(tool_calls_collected, ctx, tool_registry)
    exec_latency = int((time.time() - exec_start) * 1000)

    # Yield tool_result events 并收集 traces
    tool_traces = []
    for r in results:
        yield {
            'type': 'tool_result',
            'call_id': r.get('call_id', ''),
            'tool_name': r.get('tool_name', ''),
            'ok': r.get('ok', False),
            'latency_ms': r.get('latency_ms', 0),
            'result_preview': str(r.get('result', ''))[:200],
        }
        tool_traces.append({
            'tool_name': r.get('tool_name', ''),
            'tool_args': parse_tool_arguments(r.get('meta', {}).get('tool_args', '')),
            'result': r.get('result', ''),
            'ok': r.get('ok', False),
            'meta': r.get('meta', {}),
            'latency_ms': r.get('latency_ms', 0),
        })

    logger.info(f'[Plan] Phase 2 done: {len(results)} tools executed ({exec_latency}ms)')

    # ========================================================================
    # Phase 3: Synthesize - LLM 综合生成最终答案
    # ========================================================================
    synth_start = time.time()

    # 构建工具结果消息（OpenAI 协议格式）
    tool_messages = _build_tool_messages(tool_calls_collected, results)

    synthesize_messages = [
        {'role': 'system', 'content': SYNTHESIZER_SYSTEM_PROMPT},
        *tool_messages,
        {'role': 'user', 'content': '## 用户问题\n' + question},
    ]

    # 流式生成最终答案
    answer_parts: List[str] = []
    ttfb_ms = None
    filter_hit = None
    detected_error = ''

    try:
        for chunk in llm.stream(synthesize_messages, temperature=0.3, max_tokens=2048):
            if chunk.get('finish'):
                break
            delta = chunk.get('delta', '')
            if delta:
                if ttfb_ms is None:
                    ttfb_ms = int((time.time() - start_time) * 1000)
                    yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}

                # 流式内容审查
                if sf and filter_state:
                    outputs, hit = sf.feed(filter_state, delta)
                    if hit and hit.action == 'block':
                        filter_hit = hit
                        yield _make_filtered_event(hit)
                        break
                    for safe in outputs:
                        answer_parts.append(safe)
                        yield {'type': 'delta', 'delta': safe}
                else:
                    answer_parts.append(delta)
                    yield {'type': 'delta', 'delta': delta}
    except GeneratorExit:
        logger.info('[Plan] client aborted during Phase 3')
        raise
    except Exception as e:
        logger.exception('[Plan] Phase 3 - Synthesizer LLM failed')
        detected_error = str(e)
        if ttfb_ms is None:
            ttfb_ms = int((time.time() - start_time) * 1000)
            yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
        yield {'type': 'delta', 'delta': f'\n\n[流式中断: {e}]'}

    # Flush 审查 buffer
    if sf and filter_state and not filter_hit:
        try:
            outputs, hit = sf.flush(filter_state)
            if hit and hit.action == 'block':
                filter_hit = hit
                yield _make_filtered_event(hit)
            else:
                for safe in outputs:
                    answer_parts.append(safe)
                    yield {'type': 'delta', 'delta': safe}
        except Exception:
            logger.exception('[Plan] flush filter failed')

    # 极端情况：LLM 未产出任何 delta，补 first_token 协议
    if ttfb_ms is None:
        ttfb_ms = int((time.time() - start_time) * 1000)
        yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}

    answer = ''.join(answer_parts)
    if not answer and not filter_hit:
        answer = '[未生成内容]'

    # 收集引用
    citations, all_chunks = _collect_citations(tool_traces)

    total_ms = int((time.time() - start_time) * 1000)

    yield {
        'type': 'done',
        'answer': answer,
        'citations': citations,
        'tool_traces': tool_traces,
        'chunks': all_chunks,
        'is_filtered': filter_hit is not None,
        'filter_reason': f'output:{filter_hit.word}' if filter_hit else '',
        'detected_error': detected_error,
        'stats': {
            'total_ms': total_ms,
            'ttfb_ms': ttfb_ms,
            'plan_ms': plan_latency,
            'execute_ms': exec_latency,
            'synthesize_ms': int((time.time() - synth_start) * 1000),
            'llm': {},
            'tool_rounds': 1,
            'tool_calls_count': len(tool_calls_collected),
        },
    }


def _generate_direct_answer(llm, question: str, memory_block: str,
                            start_time: float, sf, filter_state,
                            plan_text: str = '') -> Iterator[Dict[str, Any]]:
    """规划器决定不需要工具时，直接生成答案

    如果 Phase 1 已经产出了文本内容（plan_text），直接作为答案下发；
    否则调用 LLM 生成一次纯文本回答。

    Args:
        llm: LLM 实例
        question: 用户问题
        memory_block: 历史记忆文本
        start_time: 流程起始时间
        sf: 敏感词过滤器实例（可为 None）
        filter_state: 过滤器状态（可为 None）
        plan_text: Phase 1 已产出的文本内容（可为空）
    """
    # Phase 1 已有文本：直接作为答案
    if plan_text:
        answer_parts = [plan_text]
        ttfb_ms = int((time.time() - start_time) * 1000)
        yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}

        # 审查已有文本
        safe_text = plan_text
        filter_hit = None
        if sf:
            try:
                safe_text, filter_hit = sf.check_full_text(plan_text)
            except Exception:
                pass

        if filter_hit:
            yield _make_filtered_event(filter_hit)
            answer_parts = ['']
        else:
            yield {'type': 'delta', 'delta': safe_text}
            answer_parts = [safe_text]

        yield {
            'type': 'done',
            'answer': ''.join(answer_parts),
            'citations': [],
            'tool_traces': [],
            'chunks': [],
            'is_filtered': filter_hit is not None,
            'filter_reason': f'output:{filter_hit.word}' if filter_hit else '',
            'stats': {
                'total_ms': int((time.time() - start_time) * 1000),
                'ttfb_ms': ttfb_ms,
                'llm': {},
                'tool_rounds': 0,
            },
        }
        return

    # Phase 1 无文本：调用 LLM 生成一次纯文本回答
    messages = [
        {'role': 'system', 'content': '你是「企业智能助手」，请基于你的知识简洁回答用户问题。'},
        {'role': 'user', 'content': (memory_block + '\n\n' if memory_block else '') + question},
    ]

    answer_parts: List[str] = []
    ttfb_ms = None
    filter_hit = None

    try:
        for chunk in llm.stream(messages, temperature=0.3, max_tokens=2048):
            if chunk.get('finish'):
                break
            delta = chunk.get('delta', '')
            if delta:
                if ttfb_ms is None:
                    ttfb_ms = int((time.time() - start_time) * 1000)
                    yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}

                if sf and filter_state:
                    outputs, hit = sf.feed(filter_state, delta)
                    if hit and hit.action == 'block':
                        filter_hit = hit
                        yield _make_filtered_event(hit)
                        break
                    for safe in outputs:
                        answer_parts.append(safe)
                        yield {'type': 'delta', 'delta': safe}
                else:
                    answer_parts.append(delta)
                    yield {'type': 'delta', 'delta': delta}
    except GeneratorExit:
        raise
    except Exception as e:
        logger.exception('[Plan] Direct answer generation failed')
        if ttfb_ms is None:
            ttfb_ms = int((time.time() - start_time) * 1000)
            yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
        yield {'type': 'delta', 'delta': f'\n\n[生成中断: {e}]'}

    # Flush 审查 buffer
    if sf and filter_state and not filter_hit:
        try:
            outputs, hit = sf.flush(filter_state)
            if hit and hit.action == 'block':
                filter_hit = hit
                yield _make_filtered_event(hit)
            else:
                for safe in outputs:
                    answer_parts.append(safe)
                    yield {'type': 'delta', 'delta': safe}
        except Exception:
            pass

    if ttfb_ms is None:
        ttfb_ms = int((time.time() - start_time) * 1000)
        yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}

    answer = ''.join(answer_parts)
    if not answer and not filter_hit:
        answer = '[未生成内容]'

    yield {
        'type': 'done',
        'answer': answer,
        'citations': [],
        'tool_traces': [],
        'chunks': [],
        'is_filtered': filter_hit is not None,
        'filter_reason': f'output:{filter_hit.word}' if filter_hit else '',
        'stats': {
            'total_ms': int((time.time() - start_time) * 1000),
            'ttfb_ms': ttfb_ms,
            'llm': {},
            'tool_rounds': 0,
        },
    }
