"""
Agent Executor - 问答主流程编排
- 完整链路：热点缓存 → 任务拆分判断 → 混合检索 → 记忆加载 → LLM 生成 → 记录 QA → 更新缓存
- 全链路耗时/Token/成本记录
- 拒答机制：无相关片段时降级为 general_reasoning 或 "无相关资料"
"""
import hashlib
from loguru import logger
import time
from decimal import Decimal

from django.db.models import F
from django.utils import timezone

from apps.chat.models import QaRecord, HotQaCache
from apps.memory.models import Session
from apps.memory.manager import MemoryManager
from apps.retrieval.hybrid import hybrid_search
from apps.llm.factory import get_llm
from apps.llm.prompts import build_qa_messages
from apps.system.models import LlmCallLog
from apps.llm.embedding import EmbeddingException
from apps.knowledge.access import filter_accessible_doc_ids


def _detect_error_type(llm_stats: dict) -> str:
    """根据 LLM 返回的 error 信息推断错误类型

    将 LLM 原始 error 字符串映射到结构化的 error_type，
    便于统计 timeout_rate、rate_limit_rate 等细分指标。
    使用关键词匹配规则，覆盖常见错误场景。

    Args:
        llm_stats: LLM 调用统计 dict，可能包含 error 字段

    Returns:
        error_type 字符串，空字符串表示无错误
    """
    error_msg = (llm_stats.get('error') or '').lower()
    if not error_msg:
        return ''
    if 'timeout' in error_msg or 'timed out' in error_msg:
        return 'timeout'
    if 'rate limit' in error_msg or '429' in error_msg or 'too many requests' in error_msg:
        return 'rate_limit'
    if 'connection' in error_msg or 'network' in error_msg or 'reset' in error_msg:
        return 'network'
    if 'content' in error_msg and ('filter' in error_msg or 'policy' in error_msg):
        return 'content_filter'
    if 'embedding' in error_msg:
        return 'embedding_error'
    if 'server' in error_msg or '500' in error_msg or '502' in error_msg or '503' in error_msg:
        return 'server_error'
    return 'unknown'


def _check_full_text(text: str):
    """对完整文本做一次性审查（用于缓存命中/任务拆分等非流式场景）

    与流式审查的区别：不需要缓冲窗口，一次扫描全文。
    - 命中 block：返回原文 + hit（调用方发 content_filtered 事件，不下发 delta）
    - 命中 mask：返回替换后的脱敏文本
    - 无命中：返回原文

    Returns:
        (safe_text, hit_or_none)
    """
    if not text:
        return text, None
    try:
        from apps.security.sensitive_filter import get_sensitive_filter
        sf = get_sensitive_filter()
        hits = sf.check(text)
        block_hits = [h for h in hits if h.action == 'block']
        if block_hits:
            # 命中 block：原文返回（用于审计落库），hit 标记拦截
            return text, block_hits[0]
        # 按 start 降序切片替换：避免 str.replace 的两个坑
        #   1) 子串覆盖：词库含 "ab" 和 "abc" 时，replace("ab") 先执行后 "abc" 失配
        #   2) 正则词失配：h.word 是 pattern 字符串，replace 找不到实际匹配文本
        # 降序替换保证后续替换不破坏前面 hit 的索引（与 sensitive_filter._review_buffer 一致）
        masked = text
        mask_hits = sorted([h for h in hits if h.action == 'mask'],
                           key=lambda x: x.start, reverse=True)
        for h in mask_hits:
            masked = masked[:h.start] + sf.MASK_STR + masked[h.end:]
        return masked, None
    except Exception:
        logger.exception('[executor] full text filter failed, skip')
        return text, None


def _make_filtered_event(hit) -> dict:
    """构造 content_filtered SSE 事件（与 react.py 保持一致）"""
    return {
        'type': 'content_filtered',
        'reason': '检测到违规内容，已拦截',
        'category': getattr(hit, 'category', 'other'),
    }



def _normalize(q: str) -> str:
    return ''.join(q.strip().lower().split())


def _hash(q: str) -> str:
    return hashlib.sha256(_normalize(q).encode('utf-8')).hexdigest()


def _cache_scope(user) -> str:
    """返回缓存作用域，用于区分匿名用户/超级管理员/普通用户的缓存"""
    if not user or not getattr(user, 'is_authenticated', False):
        return 'anonymous'
    if getattr(user, 'is_super_admin', False):
        return 'super'
    return f'user_{user.id}'


def ask(user, question: str, session: Session,
        root_types: list = None,
        node_ids: list = None,
        use_cache: bool = True,
        do_task_split: bool = False,
        do_rerank: bool = True,
        mode: str = 'auto') -> dict:
    """一次完整问答
    返回 QaRecord 数据 + 检索命中

    Args:
        mode: 问答模式
            - 'auto': Agent 模式，LLM 自主决定是否调用工具（默认）
            - 'rag': 传统 RAG 模式，预检索 + LLM 生成
            - 'agent': 强制 Agent 模式（与 auto 行为一致，语义明确）
    """
    from apps.agent.task_splitter import maybe_split, execute_split

    t0 = time.time()
    root_type = root_types[0] if root_types else 'company_doc'
    turn_index = (session.turn_count or 0) + 1

    # 1. 热点缓存命中
    if use_cache:
        cached = _try_cache(question, root_type, user)
        if cached:
            qa = _persist_qa(
                user=user, session=session, question=question, answer=cached['answer'],
                citations=cached.get('citations', []),
                retrieval_hits=[], retrieval_scores=[],
                stats={'latency_total_ms': int((time.time() - t0) * 1000)},
                llm_stats={}, root_type=root_type, turn_index=turn_index,
                answer_type='rag', is_hit_cache=True,
            )
            MemoryManager().append_turn(session, question, cached['answer'])
            return {'qa_id': qa.id, 'answer': cached['answer'],
                    'citations': cached.get('citations', []),
                    'chunks': [], 'is_hit_cache': True, 'stats': {'total_ms': qa.latency_total_ms}}

    # 2. Agent 模式分流（auto / agent 走 ReAct 工具调用循环）
    # Auto 模式下 LLM 通过 tool_choice='auto' 自主决定是否调用工具，
    # 不调用工具时等价于普通 chat（但无预检索 context，由 LLM 判断是否需要检索）
    if mode in ('auto', 'agent'):
        return _ask_via_agent(user, question, session, root_types, node_ids,
                              root_type, turn_index, t0)

    # 3. 任务拆分判断（仅 RAG 模式生效）
    if do_task_split:
        split = maybe_split(question)
        if split.get('need_split'):
            return execute_split(user, session, question, split, root_types=root_types)

    # 4. 混合检索
    try:
        retrieval = hybrid_search(question, user, root_types=root_types, node_ids=node_ids, do_rerank=do_rerank)
        chunks = retrieval['chunks']
        r_stats = retrieval['stats']
    except EmbeddingException as e:
        logger.error(f'[Executor] embedding failed during search: {e}')
        # 返回错误提示给前端
        answer = '当前向量服务暂时不可用，请稍后重试。'
        answer_type = 'refused'
        llm_stats = {}
        citations = []
        
        # 落 QA 记录（记录错误）
        qa = _persist_qa(
            user=user, session=session, question=question, answer=answer,
            citations=citations,
            retrieval_hits=[], retrieval_scores=[],
            stats={'latency_total_ms': int((time.time() - t0) * 1000)},
            llm_stats=llm_stats, root_type=root_type, turn_index=turn_index,
            answer_type=answer_type, is_hit_cache=False,
            # Embedding 异常标记为链路中断，但 answer_type='refused'
            # （正常的拒答类型），is_success=False 便于统计 embedding_error_rate
            error_type='embedding_error', is_success=False,
        )
        
        return {
            'qa_id': qa.id,
            'answer': answer,
            'citations': citations,
            'chunks': [],
            'is_hit_cache': False,
            'stats': {'total_ms': int((time.time() - t0) * 1000), 'error': str(e)},
        }

    # 3.5 二次权限验证：过滤用户无权访问的文档片段
    if chunks and user and getattr(user, 'is_authenticated', False):
        doc_ids = list({c['document_id'] for c in chunks})
        accessible_ids = filter_accessible_doc_ids(user, doc_ids)
        
        filtered_chunks = []
        for chunk in chunks:
            if chunk['document_id'] in accessible_ids:
                filtered_chunks.append(chunk)
        
        if filtered_chunks != chunks:
            logger.info(f'[Executor] permission filter removed {len(chunks) - len(filtered_chunks)} chunks')
            chunks = filtered_chunks

    # 4. 记忆加载
    mm = MemoryManager()
    ctx = mm.load_context(user, session, question, root_type=root_type)

    # 5. LLM 生成
    if not chunks:
        answer = '当前选择的知识库范围内未找到相关资料。请尝试选择其他知识库节点，或调整搜索关键词。'
        answer_type = 'refused'
        llm_stats = {}
    else:
        messages = build_qa_messages(question, chunks, memory_block=ctx['memory_block'])
        llm = get_llm()
        t_llm = time.time()
        resp = llm.chat(messages, temperature=0.3, max_tokens=2048)
        llm_stats = {
            'latency_llm_ms': resp.get('latency_ms', int((time.time() - t_llm) * 1000)),
            'tokens_prompt': resp.get('prompt_tokens', 0),
            'tokens_completion': resp.get('completion_tokens', 0),
            'cost': resp.get('cost', 0),
            'llm_provider': resp.get('provider', 'deepseek'),
            'llm_model': resp.get('model', 'deepseek-chat'),
        }
        answer = resp.get('content', '')
        answer_type = 'rag'
        # 记录 LlmCallLog
        try:
            LlmCallLog.objects.create(
                provider=llm_stats['llm_provider'], model=llm_stats['llm_model'],
                scene='qa', user=user if user and getattr(user, 'is_authenticated', False) else None,
                prompt_tokens=llm_stats['tokens_prompt'],
                completion_tokens=llm_stats['tokens_completion'],
                total_tokens=llm_stats['tokens_prompt'] + llm_stats['tokens_completion'],
                cost=Decimal(str(llm_stats['cost'])),
                latency_ms=llm_stats['latency_llm_ms'],
                status='error' if resp.get('error') else 'success',
                error_message=resp.get('error', '')[:1000] if resp.get('error') else '',
            )
        except Exception:
            logger.exception('llm_call_log write failed')

    # 6. 组装引用（按文档合并）
    doc_citations = {}
    for i, c in enumerate(chunks):
        doc_title = c.get('doc_title', '未知文档')
        if doc_title not in doc_citations:
            doc_citations[doc_title] = {
                'index': len(doc_citations) + 1,
                'doc_title': doc_title,
                'sections': set(),
                'pages': set(),
                'chunk_ids': []
            }
        if c.get('section_path'):
            doc_citations[doc_title]['sections'].add(c['section_path'])
        if c.get('page_number'):
            doc_citations[doc_title]['pages'].add(c['page_number'])
        doc_citations[doc_title]['chunk_ids'].append(c['chunk_id'])
    
    citations = []
    for key, val in doc_citations.items():
        citations.append({
            'index': val['index'],
            'doc_title': val['doc_title'],
            'section': ', '.join(list(val['sections'])[:3]) + ('...' if len(val['sections']) > 3 else ''),
            'page': sorted(list(val['pages']))[:5],
            'chunk_ids': val['chunk_ids']
        })

    # 7. 落 QA 记录
    total_ms = int((time.time() - t0) * 1000)
    # 通过 _detect_error_type() 从 llm_stats.error 推断错误类型，
    # is_success=False 表示链路中断（LLM 错误），区别于 answer_type='refused'（正常拒答）
    detected_error_type = _detect_error_type(llm_stats)
    qa = _persist_qa(
        user=user, session=session, question=question, answer=answer,
        citations=citations,
        retrieval_hits=[c['chunk_id'] for c in chunks],
        retrieval_scores=[
            {'chunk_id': c['chunk_id'], 'rrf': c.get('rrf_score', 0),
             'rerank': c.get('rerank_score', 0)} for c in chunks
        ],
        stats={
            'latency_retrieval_ms': r_stats.get('vector_ms', 0) + r_stats.get('bm25_ms', 0)
                                     + r_stats.get('rrf_ms', 0),
            'latency_rerank_ms': r_stats.get('rerank_ms', 0),
            'latency_total_ms': total_ms,
        },
        llm_stats=llm_stats,
        root_type=root_type, turn_index=turn_index,
        answer_type=answer_type,
        error_type=detected_error_type,
        is_success=not bool(llm_stats.get('error')),
    )

    # 8. 记录短时记忆
    mm.append_turn(session, question, answer)

    # 9. 更新热点缓存
    if answer_type == 'rag':
        _update_cache(question, root_type, user, answer, citations)

    return {
        'qa_id': qa.id,
        'answer': answer,
        'citations': citations,
        'chunks': chunks,
        'is_hit_cache': False,
        'stats': {
            'total_ms': total_ms,
            'retrieval': r_stats,
            'llm': llm_stats,
        }
    }


def _ask_via_agent(user, question, session, root_types, node_ids,
                   root_type, turn_index, t0):
    """Agent 模式同步问答入口（auto / agent 模式走这里）

    调用 agent_ask 获取答案 + 工具调用链，统一落 QaRecord + LlmCallLog + 缓存 + 记忆。
    与 RAG 模式的区别：不预检索，由 LLM 自主决定是否调用 knowledge_search 等工具。

    Returns:
        与 ask() 相同结构的返回值，额外包含 tool_traces
    """
    from apps.agent.react import agent_ask

    result = agent_ask(user, question, session, root_types=root_types,
                       node_ids=node_ids)
    answer = result['answer']
    citations = result['citations']
    chunks = result['chunks']
    tool_traces = result['tool_traces']
    llm_stats = result['llm_stats']

    # answer_type 区分：有工具调用→agent，无工具调用→general，无内容→refused
    if not answer or answer == '[未生成内容]':
        answer_type = 'refused'
    elif tool_traces:
        answer_type = 'agent'
    else:
        answer_type = 'general'

    # retrieval_hits 从 knowledge_search 命中的 chunks 提取
    retrieval_hits = [c['chunk_id'] for c in chunks]
    retrieval_scores = [
        {'chunk_id': c['chunk_id'], 'rrf': c.get('rrf_score', 0),
         'rerank': c.get('rerank_score', 0)} for c in chunks
    ]

    total_ms = int((time.time() - t0) * 1000)
    detected_error = _detect_error_type(llm_stats)
    qa = _persist_qa(
        user=user, session=session, question=question, answer=answer,
        citations=citations,
        retrieval_hits=retrieval_hits, retrieval_scores=retrieval_scores,
        stats={'latency_total_ms': total_ms, 'latency_ttfb_ms': 0},
        llm_stats=llm_stats, root_type=root_type, turn_index=turn_index,
        answer_type=answer_type, is_success=not detected_error,
        error_type=detected_error,
    )

    # 记录 Agent 工具调用链（失败不影响主流程，为 Tracing 体系铺路）
    if tool_traces:
        try:
            from apps.agent.models import AgentTrace
            AgentTrace.batch_create_from_traces(qa, user, session, tool_traces)
        except Exception:
            logger.exception('[Executor] AgentTrace batch_create failed')

    # 记录 LlmCallLog（Agent 模式可能多次调用 LLM，统计累加）
    try:
        LlmCallLog.objects.create(
            provider=llm_stats.get('llm_provider', 'deepseek'),
            model=llm_stats.get('llm_model', 'deepseek-chat'),
            scene='qa',
            user=user if user and getattr(user, 'is_authenticated', False) else None,
            prompt_tokens=llm_stats.get('tokens_prompt', 0),
            completion_tokens=llm_stats.get('tokens_completion', 0),
            total_tokens=llm_stats.get('tokens_prompt', 0) + llm_stats.get('tokens_completion', 0),
            cost=Decimal(str(llm_stats.get('cost', 0))),
            latency_ms=llm_stats.get('latency_llm_ms', 0),
            status='error' if detected_error else 'success',
        )
    except Exception:
        logger.exception('llm_call_log write failed (agent)')

    # 记忆 + 缓存（仅当有引用时更新缓存，避免无引用的通用回答被缓存）
    MemoryManager().append_turn(session, question, answer)
    if answer_type == 'agent' and citations:
        _update_cache(question, root_type, user, answer, citations)

    return {
        'qa_id': qa.id,
        'answer': answer,
        'citations': citations,
        'chunks': chunks,
        'is_hit_cache': False,
        'tool_traces': tool_traces,
        'stats': {
            'total_ms': total_ms,
            'llm': llm_stats,
            'tool_rounds': len(tool_traces),
        },
    }


def ask_stream(user, question: str, session: Session,
               root_types: list = None,
               node_ids: list = None,
               use_cache: bool = True,
               do_task_split: bool = False,
               do_rerank: bool = True,
               mode: str = 'auto'):
    """流式问答主流程，yield SSE 事件 dict

    与 ``ask`` 相同的全链路（热点缓存 → 任务拆分 → 混合检索 → 记忆 → LLM 流式生成 → 落库 → 缓存），
    但 LLM 生成阶段改走 Provider.stream，增量输出 answer。

    事件协议（前端按 type 分发）::
        {'type': 'start',       'session_id', 'citations', 'is_hit_cache'}
        {'type': 'first_token', 'ttfb_ms'}                # 首字返回耗时（请求起点→首个 delta）
        {'type': 'delta',       'delta'}                  # 增量文本
        {'type': 'done',        'message_id', 'session_id', 'citations', 'stats'}
        {'type': 'error',       'detail'}

    首字耗时 ttfb_ms：从函数入口 t0 到首个 delta yield 的毫秒数，覆盖缓存命中、检索、
    记忆加载、LLM TTFT 全链路，便于前端展示"首字返回时间"。
    """
    from apps.agent.task_splitter import maybe_split, execute_split

    t0 = time.time()
    root_type = root_types[0] if root_types else 'company_doc'
    turn_index = (session.turn_count or 0) + 1

    # 输入侧 question 审查（先于任何业务逻辑，避免浪费检索/LLM 算力）
    # 输入输出双审，"问题包含敏感词就拒答"
    q_filter_hit = None
    try:
        from apps.security.sensitive_filter import get_sensitive_filter
        _sf = get_sensitive_filter()
        _hits = _sf.check(question)
        _block = [h for h in _hits if h.action == 'block']
        if _block:
            q_filter_hit = _block[0]
    except Exception:
        logger.exception('[ask_stream] question input filter failed, skip input review')

    if q_filter_hit:
        # 输入侧命中 block：立即发 start + first_token + content_filtered + done
        # 不落缓存，不跑检索/LLM，节省算力
        ttfb = int((time.time() - t0) * 1000)
        yield {
            'type': 'start',
            'session_id': session.id,
            'citations': [],
            'is_hit_cache': False,
        }
        yield {'type': 'first_token', 'ttfb_ms': ttfb}
        yield _make_filtered_event(q_filter_hit)
        # 落 QaRecord：is_filtered=True，answer 空（无合法输出）
        qa = _persist_qa(
            user=user, session=session, question=question, answer='',
            citations=[],
            retrieval_hits=[], retrieval_scores=[],
            stats={'latency_total_ms': int((time.time() - t0) * 1000),
                   'latency_ttfb_ms': ttfb},
            llm_stats={}, root_type=root_type, turn_index=turn_index,
            answer_type='refused', is_hit_cache=False,
            is_filtered=True, filter_reason=f'input:{q_filter_hit.word}',
            # 输入侧拦截：链路完整，is_success=True 表示服务正常，
            # answer_type='refused' 表示业务拒答
            is_success=True,
        )
        yield {
            'type': 'done',
            'message_id': qa.id,
            'session_id': session.id,
            'citations': [],
            'is_filtered': True,
            'stats': {
                'total_ms': int((time.time() - t0) * 1000),
                'ttfb_ms': ttfb,
                'is_hit_cache': False,
            },
        }
        return

    # 1. 热点缓存命中：一次性输出完整答案，ttfb 即缓存命中耗时
    if use_cache:
        cached = _try_cache(question, root_type, user)
        if cached:
            ttfb_ms = int((time.time() - t0) * 1000)
            # 缓存命中也需过审：历史答案可能含违规内容（词库更新后）
            safe_answer, cache_hit = _check_full_text(cached['answer'])
            yield {
                'type': 'start',
                'session_id': session.id,
                'citations': cached.get('citations', []),
                'is_hit_cache': True,
            }
            yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
            if cache_hit:
                # 命中 block：不下发 delta，发拦截事件
                yield _make_filtered_event(cache_hit)
            else:
                yield {'type': 'delta', 'delta': safe_answer}

            qa = _persist_qa(
                user=user, session=session, question=question, answer=cached['answer'],
                citations=cached.get('citations', []),
                retrieval_hits=[], retrieval_scores=[],
                stats={'latency_total_ms': int((time.time() - t0) * 1000),
                       'latency_ttfb_ms': ttfb_ms},
                llm_stats={}, root_type=root_type, turn_index=turn_index,
                answer_type='rag', is_hit_cache=True,
                is_filtered=cache_hit is not None,
                filter_reason=(f'cache:{cache_hit.word}' if cache_hit else ''),
            )
            # 命中 block 时不应把原文写入 Memory（含违规内容，后续会绕过审查再次吐给用户）
            # mask 命中时 safe_answer 已脱敏，可安全写入
            memory_answer = '' if cache_hit else safe_answer
            MemoryManager().append_turn(session, question, memory_answer)

            yield {
                'type': 'done',
                'message_id': qa.id,
                'session_id': session.id,
                'citations': cached.get('citations', []),
                'is_filtered': cache_hit is not None,
                'stats': {
                    'total_ms': int((time.time() - t0) * 1000),
                    'ttfb_ms': ttfb_ms,
                    'is_hit_cache': True,
                },
            }
            return

    # Agent 模式分流（auto / agent 走 ReAct 流式循环）
    # 转发 agent_ask_stream 的 tool_call/tool_result/delta 事件，
    # 在 done 事件时统一落 QaRecord + 缓存 + 记忆
    if mode in ('auto', 'agent'):
        yield from _ask_stream_via_agent(user, question, session, root_types,
                                         node_ids, root_type, turn_index, t0)
        return

    # 2. 任务拆分：流式模式下不支持真流式（execute_split 内部走同步 ask），
    #    这里降级为一次性输出最终合并答案，仍保留 start/first_token/delta/done 协议
    if do_task_split:
        split = maybe_split(question)
        if split.get('need_split'):
            yield {
                'type': 'start',
                'session_id': session.id,
                'citations': [],
                'is_hit_cache': False,
            }
            try:
                result = execute_split(user, session, question, split, root_types=root_types)
            except Exception as e:
                logger.exception('[ask_stream] task_split execute error')
                yield {'type': 'error', 'detail': f'任务拆分执行失败: {e}'}
                return
            ttfb_ms = int((time.time() - t0) * 1000)
            # 任务拆分合并答案也需过审（一次性全量审查）
            safe_answer, split_hit = _check_full_text(result.get('answer', ''))
            yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
            if split_hit:
                yield _make_filtered_event(split_hit)
            else:
                yield {'type': 'delta', 'delta': safe_answer}
            yield {
                'type': 'done',
                'message_id': result.get('qa_id'),
                'session_id': session.id,
                'citations': result.get('citations', []),
                'is_filtered': split_hit is not None,
                'stats': {
                    'total_ms': int((time.time() - t0) * 1000),
                    'ttfb_ms': ttfb_ms,
                    'is_task_split': True,
                },
            }
            return

    # 3. 混合检索
    try:
        retrieval = hybrid_search(question, user, root_types=root_types, node_ids=node_ids, do_rerank=do_rerank)
        chunks = retrieval['chunks']
        r_stats = retrieval['stats']
    except EmbeddingException as e:
        logger.error(f'[ask_stream] embedding failed during search: {e}')
        answer = '当前向量服务暂时不可用，请稍后重试。'
        ttfb_ms = int((time.time() - t0) * 1000)
        yield {
            'type': 'start',
            'session_id': session.id,
            'citations': [],
            'is_hit_cache': False,
        }
        yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
        yield {'type': 'delta', 'delta': answer}
        qa = _persist_qa(
            user=user, session=session, question=question, answer=answer,
            citations=[],
            retrieval_hits=[], retrieval_scores=[],
            stats={'latency_total_ms': int((time.time() - t0) * 1000),
                   'latency_ttfb_ms': ttfb_ms},
            llm_stats={}, root_type=root_type, turn_index=turn_index,
            answer_type='refused', is_hit_cache=False,
            # Embedding 异常标记为链路中断
            error_type='embedding_error', is_success=False,
        )
        yield {
            'type': 'done',
            'message_id': qa.id,
            'session_id': session.id,
            'citations': [],
            'stats': {
                'total_ms': int((time.time() - t0) * 1000),
                'ttfb_ms': ttfb_ms,
                'error': str(e),
            },
        }
        return
    except Exception as e:
        logger.exception('[ask_stream] retrieval error')
        yield {'type': 'error', 'detail': f'检索失败: {e}'}
        return

    # 3.5 二次权限验证：过滤用户无权访问的文档片段
    if chunks and user and getattr(user, 'is_authenticated', False):
        doc_ids = list({c['document_id'] for c in chunks})
        accessible_ids = filter_accessible_doc_ids(user, doc_ids)
        filtered_chunks = [c for c in chunks if c['document_id'] in accessible_ids]
        if filtered_chunks != chunks:
            logger.info(f'[ask_stream] permission filter removed {len(chunks) - len(filtered_chunks)} chunks')
            chunks = filtered_chunks

    # 4. 记忆加载
    mm = MemoryManager()
    ctx = mm.load_context(user, session, question, root_type=root_type)

    # 5. 预先组装引用（按文档合并），在 start 事件中带出，前端可先渲染溯源区
    doc_citations = {}
    for c in chunks:
        doc_title = c.get('doc_title', '未知文档')
        if doc_title not in doc_citations:
            doc_citations[doc_title] = {
                'index': len(doc_citations) + 1,
                'doc_title': doc_title,
                'sections': set(),
                'pages': set(),
                'chunk_ids': []
            }
        if c.get('section_path'):
            doc_citations[doc_title]['sections'].add(c['section_path'])
        if c.get('page_number'):
            doc_citations[doc_title]['pages'].add(c['page_number'])
        doc_citations[doc_title]['chunk_ids'].append(c['chunk_id'])
    citations = []
    for key, val in doc_citations.items():
        citations.append({
            'index': val['index'],
            'doc_title': val['doc_title'],
            'section': ', '.join(list(val['sections'])[:3]) + ('...' if len(val['sections']) > 3 else ''),
            'page': sorted(list(val['pages']))[:5],
            'chunk_ids': val['chunk_ids']
        })

    # 6. 发送 start 事件（citations 提前下发）
    yield {
        'type': 'start',
        'session_id': session.id,
        'citations': citations,
        'is_hit_cache': False,
    }

    # 7. LLM 流式生成（或拒答降级）
    llm_stats = {}
    # 内容审查状态：filter_hit 非 None 表示命中 block，用于落库标记 is_filtered
    filter_hit = None
    if not chunks:
        # 无相关片段：直接吐拒答文案（固定文案不过审，词库不会命中）
        answer = '当前选择的知识库范围内未找到相关资料。请尝试选择其他知识库节点，或调整搜索关键词。'
        answer_type = 'refused'
        ttfb_ms = int((time.time() - t0) * 1000)
        yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
        yield {'type': 'delta', 'delta': answer}
    else:
        messages = build_qa_messages(question, chunks, memory_block=ctx['memory_block'])
        llm = get_llm()
        full_answer = []
        ttfb_ms = None
        llm_error = None
        t_llm = time.time()
        # 初始化流式审查器（失败则降级为不审查，保证服务可用）
        sf = None
        filter_state = None
        try:
            from apps.security.sensitive_filter import get_sensitive_filter
            sf = get_sensitive_filter()
            filter_state = sf.new_state()
        except Exception:
            logger.exception('[ask_stream] SensitiveFilter init failed, skip filter')

        try:
            for chunk in llm.stream(messages, temperature=0.3, max_tokens=2048):
                # finish 帧：Provider 会在最后发一帧 finish=True，可能带 error
                if chunk.get('finish'):
                    if chunk.get('error'):
                        llm_error = chunk['error']
                    break
                delta = chunk.get('delta', '')
                if delta:
                    # 流式敏感词审查：block 立即中断 / mask 脱敏后下发
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
                        # 审查未启用：原样下发
                        if ttfb_ms is None:
                            ttfb_ms = int((time.time() - t0) * 1000)
                            yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
                        full_answer.append(delta)
                        yield {'type': 'delta', 'delta': delta}
        except GeneratorExit:
            # 客户端主动终止流式：保存已生成的部分回答到 QaRecord（不能 yield，连接已断）
            logger.info(f'[ask_stream] client aborted, saving partial answer ({len(full_answer)} chars)')
            answer = ''.join(full_answer)
            try:
                _persist_qa(
                    user=user, session=session, question=question,
                    answer=answer or '[已终止]',
                    citations=citations,
                    retrieval_hits=[c['chunk_id'] for c in chunks],
                    retrieval_scores=[
                        {'chunk_id': c['chunk_id'], 'rrf': c.get('rrf_score', 0),
                         'rerank': c.get('rerank_score', 0)} for c in chunks
                    ],
                    stats={
                        'latency_retrieval_ms': r_stats.get('vector_ms', 0)
                                                 + r_stats.get('bm25_ms', 0)
                                                 + r_stats.get('rrf_ms', 0),
                        'latency_rerank_ms': r_stats.get('rerank_ms', 0),
                        'latency_total_ms': int((time.time() - t0) * 1000),
                        'latency_ttfb_ms': ttfb_ms or 0,
                    },
                    llm_stats={
                        'latency_llm_ms': int((time.time() - t_llm) * 1000),
                        'llm_provider': getattr(llm, 'name', 'deepseek'),
                        'llm_model': getattr(llm, 'model', 'deepseek-chat'),
                    },
                    root_type=root_type, turn_index=turn_index,
                    answer_type='rag' if answer else 'refused',
                    # 客户端主动终止视为成功（部分回答有效），
                    # is_success=True 防止被计入失败率统计
                    is_success=True,
                )
            except Exception:
                logger.exception('[ask_stream] failed to persist partial answer on abort')
            raise  # GeneratorExit 必须 re-raise
        except Exception as e:
            logger.exception('[ask_stream] llm stream error')
            llm_error = str(e)
            # 异常时补一个 first_token，保证前端协议完整性
            if ttfb_ms is None:
                ttfb_ms = int((time.time() - t0) * 1000)
                yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
            yield {'type': 'delta', 'delta': f'\n\n[流式中断: {e}]'}

        # 流式收尾：flush 审查 buffer，输出残余安全文本（命中 block 时跳过）
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
                logger.exception('[ask_stream] flush filter failed')

        # 极端情况：LLM 未产出任何 delta（如首帧即 finish/error），补 first_token 协议
        if ttfb_ms is None:
            ttfb_ms = int((time.time() - t0) * 1000)
            yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}

        answer = ''.join(full_answer)
        if not answer:
            answer = '[未生成内容]'
            answer_type = 'refused'
        else:
            answer_type = 'rag'

        # 流式无 token usage（DeepSeek stream 不返回 usage），仅记录耗时
        llm_stats = {
            'latency_llm_ms': int((time.time() - t_llm) * 1000),
            'tokens_prompt': 0,
            'tokens_completion': 0,
            'cost': 0,
            'llm_provider': getattr(llm, 'name', 'deepseek'),
            'llm_model': getattr(llm, 'model', 'deepseek-chat'),
            'error': llm_error,
        }
        try:
            LlmCallLog.objects.create(
                provider=llm_stats['llm_provider'], model=llm_stats['llm_model'],
                scene='qa', user=user if user and getattr(user, 'is_authenticated', False) else None,
                prompt_tokens=0, completion_tokens=0, total_tokens=0,
                cost=Decimal('0'),
                latency_ms=llm_stats['latency_llm_ms'],
                status='error' if llm_error else 'success',
                error_message=(llm_error or '')[:1000],
            )
        except Exception:
            logger.exception('llm_call_log write failed')

    # 8. 落 QaRecord
    total_ms = int((time.time() - t0) * 1000)
    # 通过 _detect_error_type() 从 llm_stats.error 推断错误类型，
    # 流式模式下 llm_stats.error 记录流式中断异常
    detected_error_type_stream = _detect_error_type(llm_stats)
    qa = _persist_qa(
        user=user, session=session, question=question, answer=answer,
        citations=citations,
        retrieval_hits=[c['chunk_id'] for c in chunks],
        retrieval_scores=[
            {'chunk_id': c['chunk_id'], 'rrf': c.get('rrf_score', 0),
             'rerank': c.get('rerank_score', 0)} for c in chunks
        ],
        stats={
            'latency_retrieval_ms': r_stats.get('vector_ms', 0) + r_stats.get('bm25_ms', 0)
                                     + r_stats.get('rrf_ms', 0),
            'latency_rerank_ms': r_stats.get('rerank_ms', 0),
            'latency_total_ms': total_ms,
            'latency_ttfb_ms': ttfb_ms,
        },
        llm_stats=llm_stats,
        root_type=root_type, turn_index=turn_index,
        answer_type=answer_type,
        error_type=detected_error_type_stream,
        is_success=not bool(llm_stats.get('error')),
        # 内容审查命中标记：is_filtered=True 不影响 is_success（审查拦截视为正常完成）
        is_filtered=filter_hit is not None,
        filter_reason=(f'output:{filter_hit.word}' if filter_hit else ''),
    )

    # 9. 记录短时记忆 + 更新热点缓存
    # 命中 block 时不更新缓存和记忆（避免违规内容被缓存复用或污染上下文）
    # 与缓存命中路径（memory_answer='' if cache_hit）和 Agent 路径（answer='' if is_filtered）保持一致
    memory_answer = '' if filter_hit else answer
    mm.append_turn(session, question, memory_answer)
    if answer_type == 'rag' and not filter_hit:
        _update_cache(question, root_type, user, answer, citations)

    # 10. 发送 done 事件
    yield {
        'type': 'done',
        'message_id': qa.id,
        'session_id': session.id,
        'citations': citations,
        'is_filtered': filter_hit is not None,
        'stats': {
            'total_ms': total_ms,
            'ttfb_ms': ttfb_ms,
            'retrieval': r_stats,
            'llm': llm_stats,
        },
    }


def _try_cache(question: str, root_type: str, user) -> dict:
    scope = _cache_scope(user)
    qh = _hash(question)
    now = timezone.now()
    scopes = [scope]
    if user and getattr(user, 'is_authenticated', False):
        scopes.append('super')
    obj = HotQaCache.objects.filter(
        question_hash=qh, root_type=root_type,
        visibility_scope__in=scopes,
    ).first()
    if not obj:
        return None
    if obj.expires_at and obj.expires_at < now:
        return None

    # 验证用户是否仍有权限访问缓存中的文档
    if user and getattr(user, 'is_authenticated', False) and obj.citations:
        doc_ids = []
        for cite in obj.citations:
            chunk_ids = cite.get('chunk_ids', [])
            if chunk_ids:
                from apps.knowledge.models import DocumentChunk
                chunk = DocumentChunk.objects.filter(id=chunk_ids[0]).first()
                if chunk and chunk.document_id not in doc_ids:
                    doc_ids.append(chunk.document_id)

        if doc_ids:
            accessible_ids = filter_accessible_doc_ids(user, doc_ids)
            if set(doc_ids) - set(accessible_ids):
                logger.info('[Cache] permission revoked for cached QA, skipping')
                return None

    HotQaCache.objects.filter(id=obj.id).update(hit_count=F('hit_count') + 1, last_hit_at=timezone.now())
    return {'answer': obj.answer, 'citations': obj.citations}


def _update_cache(question: str, root_type: str, user, answer: str, citations: list):
    scope = _cache_scope(user)
    qh = _hash(question)
    try:
        HotQaCache.objects.update_or_create(
            question_hash=qh, root_type=root_type, visibility_scope=scope,
            defaults={
                'question': question[:1000],
                'answer': answer,
                'citations': citations,
                'hit_count': 1,
            }
        )
    except Exception:
        logger.exception('cache write failed')


def _persist_qa(*, user, session, question, answer, citations,
                retrieval_hits, retrieval_scores, stats, llm_stats,
                root_type, turn_index, answer_type='rag', is_hit_cache=False,
                is_task_split=False, error_type='', is_success=True,
                is_filtered=False, filter_reason=''):
    """持久化问答记录 + 实时指标上报
    - error_type / is_success / tokens_per_second 字段填充
      error_type 分类记录 LLM/Embedding 错误原因，便于统计细分指标
      is_success=False 表示链路中断（LLM 错误、Embedding 失败等），
      区别于 answer_type='refused'（正常的"无相关资料"拒答）
      tokens_per_second 在保存时计算，避免 Dashboard 端重复计算
    - is_filtered / filter_reason 记录内容审查命中情况
      is_filtered=True 不影响 is_success（审查拦截视为正常完成，只是内容被过滤）
    - QaRecord 创建后立即调用 increment_realtime_metrics()，
      保证 Redis 实时指标与 PG 数据的一致性
    - 即使实时指标上报失败也不影响 QaRecord 保存（try/except 包裹）
    """
    # --- 计算 Token 生成速率（仅非缓存 + 成功的请求）---
    # 说明：
    # - 缓存命中：tokens_completion 是之前缓存的内容，重新计算无意义
    # - 失败场景 (is_success=False)：LLM 可能输出 0 或不完整 token，统计速率会失真
    # - llm_latency_sec 设 max(0.1) 防止除零（极快的 LLM 响应 <100ms）
    tokens_per_second = 0.0
    if not is_hit_cache and is_success:
        latency_ms = max(llm_stats.get('latency_llm_ms', 0) or 0, 0)
        completion_tokens = llm_stats.get('tokens_completion', 0) or 0
        llm_latency_sec = max(latency_ms / 1000.0, 0.1)
        if completion_tokens > 0:
            tokens_per_second = completion_tokens / llm_latency_sec

    qa = QaRecord.objects.create(
        session=session,
        user=user if user and getattr(user, 'is_authenticated', False) else None,
        turn_index=turn_index,
        question=question[:5000],
        answer=answer[:20000],
        answer_type=answer_type,
        root_type=root_type,
        retrieval_hits=retrieval_hits,
        retrieval_scores=retrieval_scores,
        citations=citations,
        latency_retrieval_ms=stats.get('latency_retrieval_ms', 0),
        latency_rerank_ms=stats.get('latency_rerank_ms', 0),
        latency_llm_ms=llm_stats.get('latency_llm_ms', 0),
        latency_total_ms=stats.get('latency_total_ms', 0),
        latency_ttfb_ms=stats.get('latency_ttfb_ms', 0),
        tokens_prompt=llm_stats.get('tokens_prompt', 0),
        tokens_completion=llm_stats.get('tokens_completion', 0),
        cost_estimate=Decimal(str(llm_stats.get('cost', 0))),
        llm_provider=llm_stats.get('llm_provider', 'deepseek'),
        llm_model=llm_stats.get('llm_model', 'deepseek-chat'),
        is_hit_cache=is_hit_cache,
        is_task_split=is_task_split,
        error_type=error_type,
        is_success=is_success,
        is_filtered=is_filtered,
        filter_reason=filter_reason[:128],
        tokens_per_second=round(tokens_per_second, 2),
        # 保留原有 error_message 写入逻辑（如有需要可后续关联 error_type）
        error_message=llm_stats.get('error', ''),
    )

    # --- 实时指标上报（Redis 原子 INCR，失败不影响主流程）---
    try:
        from apps.analytics.realtime import increment_realtime_metrics
        increment_realtime_metrics(qa)
    except Exception:
        logger.exception('[Executor] Failed to report realtime metrics (non-critical)')

    # --- 生产对话自动评估（采样 + 限速，异步触发，不阻塞用户响应）---
    # 默认关闭（PRODUCTION_EVAL_ENABLED=false）；开启后按采样率 + 令牌桶限速
    # 派发 Celery 任务做 LLM-as-judge 评估，结果落 MultiDimensionScore。
    # 放在实时指标之后、return 之前；异常不影响主对话流程。
    try:
        from apps.analytics.production_eval import maybe_dispatch_eval
        maybe_dispatch_eval(qa)
    except Exception:
        logger.exception('[Executor] Failed to dispatch production eval (non-critical)')

    return qa


def _ask_stream_via_agent(user, question, session, root_types, node_ids,
                          root_type, turn_index, t0):
    """Agent 模式流式问答入口（auto / agent 模式走这里）

    转发 agent_ask_stream 的 tool_call/tool_result/first_token/delta 事件，
    在 done 事件时统一落 QaRecord + LlmCallLog + 缓存 + 记忆，
    最后 yield 带 message_id 的 done 事件。

    与 _ask_via_agent 的区别：流式输出最终答案，且工具调用过程通过事件实时下发前端。
    """
    from apps.agent.react import agent_ask_stream

    # 先发送 start 事件（前端先渲染问答气泡 + 思考过程区）
    yield {
        'type': 'start',
        'session_id': session.id,
        'citations': [],
        'is_hit_cache': False,
        'is_agent': True,
    }

    answer_parts = []
    citations = []
    tool_traces = []
    chunks = []
    llm_stats = {}
    ttfb_ms = None
    # 内容审查命中标记（从 agent_ask_stream 的 done/content_filtered 事件提取）
    is_filtered = False
    filter_reason = ''
    # Agent 内部 error 事件标记：收到后不 return，让循环自然结束继续走落库 + done
    # 否则前端只收到 error 事件，无 done 事件（message_id 缺失导致反馈按钮不可用）
    agent_error = ''

    try:
        for event in agent_ask_stream(user, question, session,
                                       root_types=root_types, node_ids=node_ids):
            etype = event.get('type')
            if etype == 'delta':
                answer_parts.append(event.get('delta', ''))
                yield event
            elif etype == 'first_token':
                ttfb_ms = event.get('ttfb_ms')
                yield event
            elif etype in ('tool_call', 'tool_result'):
                # 工具调用过程事件直接转发（前端渲染思考过程区）
                yield event
            elif etype == 'content_filtered':
                # Agent 内部命中 block：转发拦截事件给前端
                is_filtered = True
                yield event
            elif etype == 'done':
                # 提取 agent_ask_stream 的 done 事件数据，落库后再发新 done
                citations = event.get('citations', [])
                tool_traces = event.get('tool_traces', [])
                chunks = event.get('chunks', [])
                llm_stats = event.get('stats', {}).get('llm', {})
                # 优先使用 done 事件中的 answer（避免从 delta 重建，且能正确处理 flush 后的内容）
                done_answer = event.get('answer', '')
                if done_answer:
                    answer_parts = [done_answer]
                # 同步 agent 内部的审查标记（content_filtered 事件已设置 is_filtered=True）
                if event.get('is_filtered'):
                    is_filtered = True
                    filter_reason = event.get('filter_reason', '')
            elif etype == 'error':
                # 转发 error 事件给前端，但不 return：agent_ask_stream 发完 error 会
                # return 导致 generator 结束，for 循环自然退出后继续走落库 + done 逻辑
                agent_error = event.get('detail', 'Agent 内部错误')
                yield event
    except GeneratorExit:
        # 客户端断开：保存已生成的部分答案（与 ask_stream 的处理一致）
        # 注意：若 content_filtered 事件已到达（is_filtered=True），block 命中前的部分
        # delta 仍在 answer_parts 中，落库时需标记 is_filtered 以保证审计一致性
        logger.info('[ask_stream_via_agent] client aborted, saving partial answer')
        answer = '' if is_filtered else ''.join(answer_parts)
        if answer or is_filtered:
            try:
                _persist_qa(
                    user=user, session=session, question=question,
                    answer=answer,
                    citations=citations,
                    retrieval_hits=[c['chunk_id'] for c in chunks],
                    retrieval_scores=[
                        {'chunk_id': c['chunk_id'], 'rrf': c.get('rrf_score', 0),
                         'rerank': c.get('rerank_score', 0)} for c in chunks
                    ],
                    stats={'latency_total_ms': int((time.time() - t0) * 1000),
                           'latency_ttfb_ms': ttfb_ms or 0},
                    llm_stats=llm_stats, root_type=root_type,
                    turn_index=turn_index, answer_type='agent',
                    is_success=True,
                    is_filtered=is_filtered,
                    filter_reason=filter_reason,
                )
            except Exception:
                logger.exception('[ask_stream_via_agent] failed to persist partial answer')
        raise

    # 命中审查拦截：answer 存空字符串（避免与"模型没生成内容"混淆）
    # 未命中：answer_parts 为空时兜底为 '[未生成内容]'
    if is_filtered:
        answer = ''
    else:
        answer = ''.join(answer_parts) or '[未生成内容]'

    # answer_type 区分
    if is_filtered:
        # 审查拦截：统一标记为 refused（与 ask_stream 输入侧拦截语义一致）
        answer_type = 'refused'
    elif not answer or answer == '[未生成内容]':
        answer_type = 'refused'
    elif tool_traces:
        answer_type = 'agent'
    else:
        answer_type = 'general'

    retrieval_hits = [c['chunk_id'] for c in chunks]
    retrieval_scores = [
        {'chunk_id': c['chunk_id'], 'rrf': c.get('rrf_score', 0),
         'rerank': c.get('rerank_score', 0)} for c in chunks
    ]

    total_ms = int((time.time() - t0) * 1000)
    detected_error = _detect_error_type(llm_stats)
    # Agent 内部 error 事件（如 for-else else 块 LLM 异常）：标记为链路中断
    if agent_error and not detected_error:
        detected_error = 'agent_error'
    qa = _persist_qa(
        user=user, session=session, question=question, answer=answer,
        citations=citations,
        retrieval_hits=retrieval_hits, retrieval_scores=retrieval_scores,
        stats={'latency_total_ms': total_ms, 'latency_ttfb_ms': ttfb_ms or 0},
        llm_stats=llm_stats, root_type=root_type, turn_index=turn_index,
        answer_type=answer_type, is_success=not detected_error,
        error_type=detected_error,
        # 内容审查命中标记（从 agent_ask_stream 透传）
        is_filtered=is_filtered,
        filter_reason=filter_reason,
    )

    # 记录 Agent 工具调用链（失败不影响主流程，为 Tracing 体系铺路）
    if tool_traces:
        try:
            from apps.agent.models import AgentTrace
            AgentTrace.batch_create_from_traces(qa, user, session, tool_traces)
        except Exception:
            logger.exception('[Executor] AgentTrace batch_create failed (stream)')

    # 记录 LlmCallLog
    try:
        LlmCallLog.objects.create(
            provider=llm_stats.get('llm_provider', 'deepseek'),
            model=llm_stats.get('llm_model', 'deepseek-chat'),
            scene='qa',
            user=user if user and getattr(user, 'is_authenticated', False) else None,
            prompt_tokens=llm_stats.get('tokens_prompt', 0),
            completion_tokens=llm_stats.get('tokens_completion', 0),
            total_tokens=llm_stats.get('tokens_prompt', 0) + llm_stats.get('tokens_completion', 0),
            cost=Decimal(str(llm_stats.get('cost', 0))),
            latency_ms=llm_stats.get('latency_llm_ms', 0),
            status='error' if detected_error else 'success',
        )
    except Exception:
        logger.exception('llm_call_log write failed (agent stream)')

    # 记忆 + 缓存（命中 block 时不更新缓存，避免违规内容被缓存复用）
    MemoryManager().append_turn(session, question, answer)
    if answer_type == 'agent' and citations and not is_filtered:
        _update_cache(question, root_type, user, answer, citations)

    yield {
        'type': 'done',
        'message_id': qa.id,
        'session_id': session.id,
        'citations': citations,
        'tool_traces': tool_traces,
        'is_filtered': is_filtered,
        'stats': {
            'total_ms': total_ms,
            'ttfb_ms': ttfb_ms,
            'llm': llm_stats,
            'tool_rounds': len(tool_traces),
            'is_agent': True,
        },
    }
