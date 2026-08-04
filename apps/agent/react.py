"""
ReAct Agent 循环 - Agentic RAG 核心
- agent_ask: 同步 ReAct 循环（LLM 决策 → 工具调用 → 结果回填 → 再决策）
- agent_ask_stream: 流式 ReAct 循环（yield SSE 事件，含 tool_call/tool_result/delta）

与 executor.ask/ask_stream 的区别：
- 不预检索 context，而是让 LLM 自主决定是否调用 knowledge_search 等工具
- 支持多轮工具调用（最多 MAX_TOOL_ROUNDS 轮，防止无限循环）
- 收集工具调用链（tool_traces）供 AgentTrace 记录和前端展示

事件协议（流式）：
    {'type': 'tool_call',  'call_id', 'tool_name', 'tool_args'}
    {'type': 'tool_result','call_id', 'tool_name', 'ok', 'latency_ms', 'result_preview'}
    {'type': 'first_token', 'ttfb_ms'}
    {'type': 'delta',       'delta'}
    {'type': 'done',        'message_id'(由 executor 填), 'citations', 'tool_traces', 'stats'}
    {'type': 'error',       'detail'}
"""
import time
from typing import Any, Dict, Iterator, List

from loguru import logger

from apps.llm.factory import get_llm
from apps.llm.prompts.agent import build_agent_messages
from apps.memory.manager import MemoryManager

from .tools import ToolContext, get_default_registry, parse_tool_arguments


# 最大工具调用轮数（防止 LLM 陷入无限调用循环）
MAX_TOOL_ROUNDS = 5
# 单个工具结果回填给 LLM 的最大长度（防止 context 爆炸）
MAX_TOOL_RESULT_CHARS = 4000


def _make_filtered_event(hit) -> Dict[str, Any]:
    """构造 content_filtered SSE 事件

    前端收到后：清空已展示内容 + 显示"违规已拦截"提示卡片（含误判反馈按钮）。
    不暴露具体命中词（避免二次传播违规内容），仅返回 category 供运营分析。
    """
    return {
        'type': 'content_filtered',
        'reason': '检测到违规内容，已拦截',
        'category': getattr(hit, 'category', 'other'),
    }


def _build_tool_messages(tool_calls: List[Dict], results: List[Dict]) -> List[Dict]:
    """构建工具调用的 assistant message + tool result messages

    OpenAI 协议要求：
    1. assistant message 必须包含 tool_calls 字段（原始结构）
    2. 每个 tool_call 对应一条 role='tool' 的 message，tool_call_id 关联

    Args:
        tool_calls: [{'id', 'name', 'arguments'}]
        results: [{'call_id', 'result', 'ok'}]（与 tool_calls 顺序一致）

    Returns:
        messages 列表（1 条 assistant + N 条 tool）
    """
    messages = []
    # assistant message（含 tool_calls，OpenAI 协议格式）
    messages.append({
        'role': 'assistant',
        'content': None,
        'tool_calls': [
            {
                'id': tc['id'],
                'type': 'function',
                'function': {'name': tc['name'], 'arguments': tc['arguments']},
            }
            for tc in tool_calls
        ],
    })
    # 每个工具调用结果对应一条 tool message
    for i, tc in enumerate(tool_calls):
        result = results[i]
        content = result.get('result', '')
        # 截断过长的工具结果，防止 context 膨胀
        if len(content) > MAX_TOOL_RESULT_CHARS:
            content = content[:MAX_TOOL_RESULT_CHARS] + '\n...（结果已截断）'
        messages.append({
            'role': 'tool',
            'tool_call_id': tc['id'],
            'content': content,
        })
    return messages


def _collect_citations(tool_traces: List[Dict]) -> tuple:
    """从工具调用链收集知识库引用

    遍历所有 knowledge_search 工具调用的返回结果，收集命中的 chunks，
    按文档合并组装为 citations（与 executor.py 的引用格式一致）。

    Returns:
        (citations, all_chunks)
        - citations: 按文档合并的引用列表
        - all_chunks: 所有命中的 chunk 列表（用于 retrieval_hits 记录）
    """
    doc_citations: Dict[str, Dict] = {}
    all_chunks: List[Dict] = []

    for trace in tool_traces:
        if trace.get('tool_name') != 'knowledge_search':
            continue
        meta = trace.get('meta') or {}
        chunks = meta.get('chunks') or []
        for c in chunks:
            # 按 chunk_id 去重（同一次调用不会重复，多次调用可能命中同一 chunk）
            if c.get('chunk_id') in [x.get('chunk_id') for x in all_chunks]:
                continue
            all_chunks.append(c)
            doc_title = c.get('doc_title', '未知文档')
            if doc_title not in doc_citations:
                doc_citations[doc_title] = {
                    'index': len(doc_citations) + 1,
                    'doc_title': doc_title,
                    'sections': set(),
                    'pages': set(),
                    'chunk_ids': [],
                }
            if c.get('section_path'):
                doc_citations[doc_title]['sections'].add(c['section_path'])
            if c.get('page_number'):
                doc_citations[doc_title]['pages'].add(c['page_number'])
            doc_citations[doc_title]['chunk_ids'].append(c.get('chunk_id'))

    citations = []
    for val in doc_citations.values():
        citations.append({
            'index': val['index'],
            'doc_title': val['doc_title'],
            'section': ', '.join(list(val['sections'])[:3]) +
                       ('...' if len(val['sections']) > 3 else ''),
            'page': sorted(list(val['pages']))[:5],
            'chunk_ids': val['chunk_ids'],
        })
    return citations, all_chunks


def _execute_tool_calls(tool_calls: List[Dict], ctx: ToolContext,
                        tool_registry) -> List[Dict]:
    """执行一批工具调用

    Args:
        tool_calls: LLM 返回的 tool_calls 列表 [{'id', 'name', 'arguments'}]
        ctx: 工具执行上下文
        tool_registry: 工具注册表

    Returns:
        [{'call_id', 'result', 'ok', 'meta', 'latency_ms', 'tool_name'}]
    """
    results = []
    for tc in tool_calls:
        call_id = tc.get('id', '')
        name = tc.get('name', '')
        raw_args = tc.get('arguments', '')
        args = parse_tool_arguments(raw_args)
        ret = tool_registry.execute(name, args, ctx)
        ret['call_id'] = call_id
        results.append(ret)
    return results


def agent_ask(user, question: str, session, root_types: list = None,
              node_ids: list = None) -> Dict[str, Any]:
    """同步 ReAct Agent 问答

    流程：
    1. 热点缓存命中检测（复用 executor 的缓存逻辑，由调用方处理）
    2. 加载记忆 + 构建初始 messages
    3. 循环调用 LLM（带 tools），处理 tool_calls
    4. 最终 LLM 返回纯文本答案
    5. 收集引用 + 工具调用链

    注意：本函数不落 QaRecord、不更新缓存，由 executor.ask 统一处理。

    Returns:
        {'answer': str, 'citations': list, 'chunks': list,
         'tool_traces': list, 'llm_stats': dict}
    """
    t0 = time.time()
    tool_registry = get_default_registry()
    openai_tools = tool_registry.to_openai_tools()
    ctx = ToolContext(user=user, session=session, root_types=root_types,
                      node_ids=node_ids, llm=get_llm())

    # 加载记忆
    mm = MemoryManager()
    root_type = root_types[0] if root_types else 'company_doc'
    mem_ctx = mm.load_context(user, session, question, root_type=root_type)
    messages = build_agent_messages(question, memory_block=mem_ctx['memory_block'])

    llm = get_llm()
    tool_traces: List[Dict] = []
    total_llm_stats = {
        'latency_llm_ms': 0, 'tokens_prompt': 0, 'tokens_completion': 0,
        'cost': 0, 'llm_provider': getattr(llm, 'name', 'deepseek'),
        'llm_model': getattr(llm, 'model', 'deepseek-chat'),
    }

    for round_idx in range(MAX_TOOL_ROUNDS):
        resp = llm.chat(messages, temperature=0.3, max_tokens=2048,
                        tools=openai_tools, tool_choice='auto')

        # 累加 LLM 调用统计
        total_llm_stats['latency_llm_ms'] += resp.get('latency_ms', 0)
        total_llm_stats['tokens_prompt'] += resp.get('prompt_tokens', 0)
        total_llm_stats['tokens_completion'] += resp.get('completion_tokens', 0)
        total_llm_stats['cost'] += resp.get('cost', 0)

        tool_calls = resp.get('tool_calls') or []
        if not tool_calls:
            # 无工具调用：LLM 直接给出最终答案
            answer = resp.get('content', '') or '[未生成内容]'
            break

        # 有工具调用：执行工具，回填结果，继续循环
        results = _execute_tool_calls(tool_calls, ctx, tool_registry)
        for i, tc in enumerate(tool_calls):
            r = results[i]
            tool_traces.append({
                'round': round_idx + 1,
                'call_id': tc.get('id', ''),
                'tool_name': tc.get('name', ''),
                'tool_args': parse_tool_arguments(tc.get('arguments', '')),
                'result': r.get('result', ''),
                'ok': r.get('ok', False),
                'meta': r.get('meta', {}),
                'latency_ms': r.get('latency_ms', 0),
            })
            logger.info(f'[Agent] round {round_idx + 1} tool {tc.get("name")} ok={r.get("ok")} latency={r.get("latency_ms")}ms')

        # 把 assistant tool_calls + tool results 加入 messages
        messages.extend(_build_tool_messages(tool_calls, results))
    else:
        # 达到最大轮数仍未给出最终答案：强制要求 LLM 总结
        logger.warning(f'[Agent] reached MAX_TOOL_ROUNDS={MAX_TOOL_ROUNDS}, forcing final answer')
        messages.append({
            'role': 'user',
            'content': '已达到工具调用上限，请基于已获取的信息直接回答用户问题，不要再调用工具。',
        })
        resp = llm.chat(messages, temperature=0.3, max_tokens=2048)
        total_llm_stats['latency_llm_ms'] += resp.get('latency_ms', 0)
        total_llm_stats['tokens_prompt'] += resp.get('prompt_tokens', 0)
        total_llm_stats['tokens_completion'] += resp.get('completion_tokens', 0)
        total_llm_stats['cost'] += resp.get('cost', 0)
        answer = resp.get('content', '') or '[未生成内容]'

    # 收集引用
    citations, all_chunks = _collect_citations(tool_traces)
    total_llm_stats['latency_total_ms'] = int((time.time() - t0) * 1000)

    return {
        'answer': answer,
        'citations': citations,
        'chunks': all_chunks,
        'tool_traces': tool_traces,
        'llm_stats': total_llm_stats,
    }


def agent_ask_stream(user, question: str, session, root_types: list = None,
                     node_ids: list = None) -> Iterator[Dict[str, Any]]:
    """流式 ReAct Agent 问答

    yield SSE 事件：
    - tool_call / tool_result：工具调用过程（前端渲染"思考过程"区）
    - first_token / delta：最终答案的流式文本
    - done：结束（含 citations / tool_traces / stats）

    与 agent_ask 的区别：
    - 工具调用阶段不产生文本 delta
    - 最后一轮 LLM 调用走 stream，增量输出最终答案
    - ttfb_ms 从首个最终答案 delta 开始算（不含工具调用耗时）
    """
    t0 = time.time()

    # 0. 输入侧 question 审查（先于任何业务逻辑，避免浪费工具调用/LLM 算力）
    # 与 ask_stream 保持一致：输入输出双审，用户心智模型是"问题包含敏感词就拒答"
    q_filter_hit = None
    try:
        from apps.security.sensitive_filter import get_sensitive_filter
        _sf = get_sensitive_filter()
        _hits = _sf.check(question)
        _block = [h for h in _hits if h.action == 'block']
        if _block:
            q_filter_hit = _block[0]
    except Exception:
        logger.exception('[agent_ask_stream] question input filter failed, skip input review')

    if q_filter_hit:
        # 输入侧命中 block：first_token + content_filtered + done 直接返回
        # 注意：start 事件已由调用方 _ask_stream_via_agent 统一发送，这里不要重复发
        ttfb = int((time.time() - t0) * 1000)
        yield {'type': 'first_token', 'ttfb_ms': ttfb}
        yield _make_filtered_event(q_filter_hit)
        # 发 done 事件并标记 is_filtered=True，供 _ask_stream_via_agent 透传落库
        # 字段与正常路径 done 事件保持一致（answer='' / ttfb_ms / tool_rounds=0）
        yield {
            'type': 'done',
            'answer': '',
            'citations': [],
            'tool_traces': [],
            'chunks': [],
            'is_filtered': True,
            'filter_reason': f'input:{q_filter_hit.word}',
            'stats': {
                'total_ms': int((time.time() - t0) * 1000),
                'ttfb_ms': ttfb,
                'llm': {},
                'tool_rounds': 0,
            },
        }
        return

    tool_registry = get_default_registry()
    openai_tools = tool_registry.to_openai_tools()
    ctx = ToolContext(user=user, session=session, root_types=root_types,
                      node_ids=node_ids, llm=get_llm())

    # 加载记忆
    mm = MemoryManager()
    root_type = root_types[0] if root_types else 'company_doc'
    mem_ctx = mm.load_context(user, session, question, root_type=root_type)
    messages = build_agent_messages(question, memory_block=mem_ctx['memory_block'])

    llm = get_llm()
    tool_traces: List[Dict] = []
    total_llm_stats = {
        'latency_llm_ms': 0, 'tokens_prompt': 0, 'tokens_completion': 0,
        'cost': 0, 'llm_provider': getattr(llm, 'name', 'deepseek'),
        'llm_model': getattr(llm, 'model', 'deepseek-chat'),
    }
    ttfb_ms = None
    full_answer: List[str] = []
    # 内容审查状态：sf 为 None 表示未启用或词库为空
    # filter_hit 非 None 时表示命中 block，循环结束后用于 done 事件标记 is_filtered
    sf = None
    filter_state = None
    filter_hit = None
    try:
        from apps.security.sensitive_filter import get_sensitive_filter
        sf = get_sensitive_filter()
        filter_state = sf.new_state()
    except Exception:
        logger.exception('[agent_ask_stream] SensitiveFilter init failed, skip filter')

    for round_idx in range(MAX_TOOL_ROUNDS):
        # 最后一轮如果已无工具调用，走纯流式生成最终答案
        # 前几轮用 stream + tools，让 LLM 决策是否调用工具
        is_last_round = (round_idx == MAX_TOOL_ROUNDS - 1)
        stream_kwargs = {'tools': openai_tools, 'tool_choice': 'auto'}
        if is_last_round:
            # 最后一轮强制不带 tools，要求 LLM 直接生成答案
            stream_kwargs = {}

        round_tool_calls: List[Dict] = []
        round_latency = 0

        try:
            for chunk in llm.stream(messages, temperature=0.3, max_tokens=2048,
                                    **stream_kwargs):
                if chunk.get('finish'):
                    round_latency = chunk.get('latency_ms', 0)
                    round_tool_calls = chunk.get('tool_calls') or []
                    break
                delta = chunk.get('delta', '')
                if delta:
                    # 流式敏感词审查：block 立即中断 / mask 脱敏后下发 / 无命中保留窗口
                    if sf and filter_state:
                        outputs, hit = sf.feed(filter_state, delta)
                        if hit and hit.action == 'block':
                            # 命中 block：发 content_filtered 事件，立即终止 LLM 流
                            filter_hit = hit
                            yield _make_filtered_event(hit)
                            break
                        # 把审查后的安全片段逐个下发（mask 命中已被替换为 ***）
                        for safe in outputs:
                            if ttfb_ms is None and not round_tool_calls:
                                ttfb_ms = int((time.time() - t0) * 1000)
                                yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
                            full_answer.append(safe)
                            yield {'type': 'delta', 'delta': safe}
                    else:
                        # 审查未启用：原样下发
                        if ttfb_ms is None and not round_tool_calls:
                            ttfb_ms = int((time.time() - t0) * 1000)
                            yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
                        full_answer.append(delta)
                        yield {'type': 'delta', 'delta': delta}
        except GeneratorExit:
            # 客户端断开：保存已生成内容（由 executor 的 finally 处理落库）
            logger.info(f'[agent_ask_stream] client aborted at round {round_idx + 1}')
            raise
        except Exception as e:
            logger.exception(f'[agent_ask_stream] llm stream error at round {round_idx + 1}')
            # 异常路径也尝试 flush，避免已审查安全内容滞留 buffer 丢失
            if sf and filter_state and not filter_hit:
                try:
                    outputs, _ = sf.flush(filter_state)
                    for safe in outputs:
                        if ttfb_ms is None:
                            ttfb_ms = int((time.time() - t0) * 1000)
                            yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
                        yield {'type': 'delta', 'delta': safe}
                except Exception:
                    logger.exception('[agent_ask_stream] flush on error failed')
            yield {'type': 'error', 'detail': f'LLM 流式调用失败: {e}'}
            return

        # 命中 block：跳出整个 ReAct 循环（不再继续生成）
        if filter_hit:
            break

        total_llm_stats['latency_llm_ms'] += round_latency

        # 无工具调用即结束循环：LLM 已直接回答（有文本）或异常返回（无文本）
        # （full_answer 已在 delta 循环中逐片段 append，无需重复 extend）
        if not round_tool_calls:
            break

        # 有工具调用：执行工具，回填结果，继续下一轮
        # 先发送 tool_call 事件（前端渲染思考过程）
        for tc in round_tool_calls:
            yield {
                'type': 'tool_call',
                'call_id': tc.get('id', ''),
                'tool_name': tc.get('name', ''),
                'tool_args': parse_tool_arguments(tc.get('arguments', '')),
            }

        results = _execute_tool_calls(round_tool_calls, ctx, tool_registry)
        for i, tc in enumerate(round_tool_calls):
            r = results[i]
            tool_traces.append({
                'round': round_idx + 1,
                'call_id': tc.get('id', ''),
                'tool_name': tc.get('name', ''),
                'tool_args': parse_tool_arguments(tc.get('arguments', '')),
                'result': r.get('result', ''),
                'ok': r.get('ok', False),
                'meta': r.get('meta', {}),
                'latency_ms': r.get('latency_ms', 0),
            })
            # 发送 tool_result 事件（result 截断预览，避免事件过大）
            result_preview = r.get('result', '')
            if len(result_preview) > 500:
                result_preview = result_preview[:500] + '...'
            yield {
                'type': 'tool_result',
                'call_id': tc.get('id', ''),
                'tool_name': tc.get('name', ''),
                'ok': r.get('ok', False),
                'latency_ms': r.get('latency_ms', 0),
                'result_preview': result_preview,
            }
            logger.info(f'[Agent stream] round {round_idx + 1} tool {tc.get("name")} ok={r.get("ok")} latency={r.get("latency_ms")}ms')

        # 把 assistant tool_calls + tool results 加入 messages，继续下一轮
        messages.extend(_build_tool_messages(round_tool_calls, results))
    else:
        # 防御性后备：正常情况下 is_last_round 逻辑（第 5 轮不带 tools）会保证
        # LLM 直接生成答案并 break，不会走到 else。此块仅防 LLM provider 在
        # 无 tools 参数时仍返回 tool_calls 的极端异常，避免无限循环。
        logger.warning('[Agent stream] reached MAX_TOOL_ROUNDS, forcing final answer')
        messages.append({
            'role': 'user',
            'content': '已达到工具调用上限，请基于已获取的信息直接回答用户问题，不要再调用工具。',
        })
        try:
            for chunk in llm.stream(messages, temperature=0.3, max_tokens=2048):
                if chunk.get('finish'):
                    total_llm_stats['latency_llm_ms'] += chunk.get('latency_ms', 0)
                    break
                delta = chunk.get('delta', '')
                if delta:
                    # 强制最终答案阶段同样过审
                    if sf and filter_state:
                        outputs, hit = sf.feed(filter_state, delta)
                        if hit and hit.action == 'block':
                            filter_hit = hit
                            yield _make_filtered_event(hit)
                            break
                        for safe in outputs:
                            if ttfb_ms is None:
                                ttfb_ms = int((time.time() - t0) * 1000)
                                yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
                            full_answer.append(safe)
                            yield {'type': 'delta', 'delta': safe}
                    else:
                        if ttfb_ms is None:
                            ttfb_ms = int((time.time() - t0) * 1000)
                            yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
                        full_answer.append(delta)
                        yield {'type': 'delta', 'delta': delta}
        except GeneratorExit:
            raise
        except Exception as e:
            logger.exception('[agent_ask_stream] final answer stream error')
            # 异常路径也尝试 flush
            if sf and filter_state and not filter_hit:
                try:
                    outputs, _ = sf.flush(filter_state)
                    for safe in outputs:
                        if ttfb_ms is None:
                            ttfb_ms = int((time.time() - t0) * 1000)
                            yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
                        yield {'type': 'delta', 'delta': safe}
                except Exception:
                    logger.exception('[agent_ask_stream] final flush on error failed')
            yield {'type': 'error', 'detail': f'最终答案生成失败: {e}'}
            return

    # 流式收尾：flush 审查 buffer，输出残余安全文本
    if sf and filter_state and not filter_hit:
        try:
            outputs, hit = sf.flush(filter_state)
            if hit and hit.action == 'block':
                filter_hit = hit
                yield _make_filtered_event(hit)
            else:
                for safe in outputs:
                    full_answer.append(safe)
                    yield {'type': 'delta', 'delta': safe}
        except Exception:
            logger.exception('[agent_ask_stream] flush filter failed')

    answer = ''.join(full_answer)
    citations, all_chunks = _collect_citations(tool_traces)

    # 如果全程没产生文本 delta（极端情况），补 first_token
    if ttfb_ms is None:
        ttfb_ms = int((time.time() - t0) * 1000)
        yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}

    yield {
        'type': 'done',
        'answer': answer,  # 完整答案（供 _ask_stream_via_agent 直接使用，避免从 delta 重建）
        'citations': citations,
        'tool_traces': tool_traces,
        'chunks': all_chunks,
        'is_filtered': filter_hit is not None,
        'filter_reason': (f'output:{filter_hit.word}' if filter_hit else ''),
        'stats': {
            'total_ms': int((time.time() - t0) * 1000),
            'ttfb_ms': ttfb_ms,
            'llm': total_llm_stats,
            'tool_rounds': len(tool_traces),
        },
    }
