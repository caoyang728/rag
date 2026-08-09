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
from apps.retrieval.query_transform import build_route_trace
from apps.llm.factory import get_llm
from apps.llm.prompts import build_qa_messages
from apps.system.models import LlmCallLog
from apps.llm.embedding import EmbeddingException
from apps.knowledge.access import filter_accessible_doc_ids, build_user_context


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


def _collect_transform_route_trace(tool_traces: list) -> list:
    """从 Agent 工具调用链 meta 中收集查询改写/分解追踪信息，转成 route_trace 审计条目

    knowledge_search 工具执行时若命中改写/分解链路，会把 transform 追踪信息放进
    工具结果 meta，这里统一收集并落 QaRecord.route_trace，供评估看板统计"改写命中率"。
    """
    trace = []
    for t in tool_traces or []:
        tr = (t.get('meta') or {}).get('transform')
        if tr and tr.get('enabled'):
            trace.extend(build_route_trace(tr))
    return trace



def _normalize(q: str) -> str:
    return ''.join(q.strip().lower().split())


def _hash(q: str) -> str:
    return hashlib.sha256(_normalize(q).encode('utf-8')).hexdigest()


def _build_org_scope(doc_ids: list) -> str:
    """计算引用文档的权限组（缓存分组标识）

    权限组 = 答案引用文档的组织归属：
    - 无引用（纯 LLM 知识）或引用全为 PUBLIC 可见文档 → 'public'（任意用户可命中）
    - 否则 → 'org_d3_t7'（部门 d / 团队 t ID 升序拼接），同一权限组用户共享该条缓存

    黑名单 / 个人共享 / 申请审批等个人级权限无法表达在组织组内，
    由命中时的文档级兜底校验（_cache_docs_accessible）兜住，遵循 Deny Override 铁律。
    """
    if not doc_ids:
        return 'public'
    from apps.knowledge.models import Document, VisibilityLevel
    orgs = set()
    for dept_id, team_id, vlevel in (Document.objects.filter(id__in=doc_ids)
                                     .values_list('dept_id', 'team_id', 'visibility_level')):
        # PUBLIC 全局可见文档不产生组织归属，纯 PUBLIC 引用整组降级为 'public'
        if vlevel == VisibilityLevel.PUBLIC:
            continue
        if team_id:
            orgs.add(f't{team_id}')
        elif dept_id:
            orgs.add(f'd{dept_id}')
    if not orgs:
        return 'public'
    return 'org_' + '_'.join(sorted(orgs))


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

    # 三层路由分流（wiki / graphrag / rag 走 LLM Wiki → GraphRAG → RAG 兜底）
    # 检索阶段同步完成（orchestrate），LLM 生成阶段流式输出
    if mode in ('wiki', 'graphrag', 'rag'):
        yield from _ask_stream_via_route(user, question, session, root_types,
                                         root_type, turn_index, t0)
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
        # 查询改写/分解审计：transform 追踪信息记入 QaRecord.route_trace
        # 开关关闭时 retrieval 无 'transform' 键，route_trace 保持 None（与现状一致）
        transform = retrieval.get('transform') or {}
        transform_route_trace = build_route_trace(transform) or None
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
                    route_trace=transform_route_trace,
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
        route_trace=transform_route_trace,
    )

    # 9. 记录短时记忆 + 更新热点缓存
    # 命中 block 时不更新缓存和记忆（避免违规内容被缓存复用或污染上下文）
    # 与缓存命中路径（memory_answer='' if cache_hit）和 Agent 路径（answer='' if is_filtered）保持一致
    memory_answer = '' if filter_hit else answer
    mm.append_turn(session, question, memory_answer)
    if _should_update_cache(answer_type, filter_hit is not None):
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


def _ask_stream_via_route(user, question, session, root_types, root_type,
                          turn_index, t0):
    """三层路由流式问答（wiki / graphrag / rag 模式）

    检索阶段（orchestrate）同步完成，LLM 生成阶段流式输出，
    事件协议与 ask_stream 一致：start → first_token → delta* → done。
    输出侧沿用流式敏感词审查（block 中断 / mask 脱敏），
    并在 done 事件中携带 route_source / route_trace 供前端展示路由链路。
    """
    from apps.graph.router import orchestrate
    from apps.llm.prompts.qa import QA_USER_TEMPLATE, SYSTEM_PROMPT

    # 1. 三层路由决策（同步检索）
    route_result = orchestrate(question, user, session)
    context = route_result.get('context', '')
    chunks = route_result.get('chunks', [])
    route_source = route_result.get('source', 'none')
    route_trace = route_result.get('route_trace', [])
    citations = _build_citations(chunks)

    # 2. start 事件（引用提前下发，前端可先渲染溯源区）
    yield {
        'type': 'start',
        'session_id': session.id,
        'citations': citations,
        'is_hit_cache': False,
        'route_source': route_source,
        'route_trace': route_trace,
    }

    mm = MemoryManager()
    llm_stats = {}
    filter_hit = None

    # 3. 无上下文：拒答（与 ask_stream RAG 分支语义一致）
    if not context:
        answer = '当前知识范围内未找到相关资料，无法回答该问题。'
        answer_type = 'refused'
        ttfb_ms = int((time.time() - t0) * 1000)
        yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
        yield {'type': 'delta', 'delta': answer}
    else:
        # 4. 记忆加载 + 构造 messages（context 为路由层格式化文本，直接注入 QA 模板）
        ctx = mm.load_context(user, session, question, root_type=root_type)
        user_content = QA_USER_TEMPLATE.format(
            memory_block=ctx['memory_block'] or '（无历史记忆）',
            context_block=context,
            question=question,
        )
        messages = [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': user_content},
        ]

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
            # 客户端主动终止流式：保存已生成的部分回答（不能 yield，连接已断）
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

        # 极端情况：LLM 未产出任何 delta，补 first_token 协议
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

    # 5. 落 QaRecord
    total_ms = int((time.time() - t0) * 1000)
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
            'latency_total_ms': total_ms,
            'latency_ttfb_ms': ttfb_ms,
        },
        llm_stats=llm_stats,
        root_type=root_type, turn_index=turn_index,
        answer_type=answer_type,
        error_type=detected_error_type,
        is_success=not bool(llm_stats.get('error')),
        is_filtered=filter_hit is not None,
        filter_reason=(f'output:{filter_hit.word}' if filter_hit else ''),
        route_source=route_source,
        route_trace=route_trace,
    )

    # 6. 记忆 + 缓存（命中 block 时不更新，避免违规内容被缓存复用或污染上下文）
    memory_answer = '' if filter_hit else answer
    mm.append_turn(session, question, memory_answer)
    if _should_update_cache(answer_type, filter_hit is not None):
        _update_cache(question, root_type, user, answer, citations)

    # 7. done 事件（带路由链路信息）
    yield {
        'type': 'done',
        'message_id': qa.id,
        'session_id': session.id,
        'citations': citations,
        'is_filtered': filter_hit is not None,
        'route_source': route_source,
        'route_trace': route_trace,
        'stats': {
            'total_ms': total_ms,
            'ttfb_ms': ttfb_ms,
            'route_source': route_source,
            'route_trace': route_trace,
            'llm': llm_stats,
        },
    }


def _build_citations(chunks: list) -> list:
    """按文档合并 chunks，组装引用列表（ask / ask_stream / 三层路由共用）

    同一文档的多个片段合并为一条引用，记录章节与页码集合。

    Args:
        chunks: hybrid_search 返回的 chunks 列表

    Returns:
        [{'index','doc_title','section','page','chunk_ids'}]
    """
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
    return citations


def _user_covers_org_scope(user, org_scope: str) -> bool:
    """校验用户可见组织范围是否覆盖缓存权限组（AND 全覆盖）

    org_scope 形如 'org_d3_t7'（d=部门，t=团队）。
    覆盖规则：
    - 部门：须在用户可见部门集合（管辖 + 所属部门祖先链）中；
    - 团队：在用户可见团队集合（管辖团队 + 自己所属团队）中，
      或其所属部门在可见部门集合中（部门级可见天然覆盖下属团队）。
    粗筛不做个人级（黑名单/个人共享）判定，由文档级兜底校验兜底。
    """
    ctx = build_user_context(user)
    if not ctx:
        return False
    visible_depts = ctx['visible_depts']
    # 普通用户对自己所属团队的 TEAM_ONLY 文档自然可见，纳入可见团队集合
    visible_teams = set(ctx['visible_teams'])
    if getattr(user, 'team_id', None):
        visible_teams.add(user.team_id)
    for token in org_scope.split('_')[1:]:
        kind, oid = token[0], int(token[1:])
        if kind == 'd':
            if oid not in visible_depts:
                return False
        else:
            if oid in visible_teams:
                continue
            from apps.users.models import Team
            # 团队所属部门在用户可见部门中 → 该团队被部门级可见覆盖
            if Team.objects.filter(id=oid, department_id__in=visible_depts).exists():
                continue
            return False
    return True


def _cache_scope_accessible(user, obj) -> bool:
    """权限组粗筛：用户是否有资格进入该缓存分组

    - 'public'：无引用或全 PUBLIC 文档，任意用户可命中（无需校验）；
    - 'org_...'：组织覆盖校验（超管全权限直接通过）；
    - 'super'/'anonymous'：历史分组，仅身份匹配（新写入不再产生）；
    - 其他（旧 per-user 分组）：直接失效。
    """
    scope = obj.visibility_scope
    if scope == 'public':
        return True
    if scope.startswith('org_'):
        if not user or not getattr(user, 'is_authenticated', False):
            return False
        if getattr(user, 'is_super_admin', False):
            return True
        return _user_covers_org_scope(user, scope)
    if scope == 'super':
        return bool(user and getattr(user, 'is_super_admin', False))
    if scope == 'anonymous':
        return not (user and getattr(user, 'is_authenticated', False))
    return False


def _cache_docs_accessible(user, obj) -> bool:
    """文档级兜底：引用文档全部可访问才返回缓存

    组织粗筛看不见个人级权限（黑名单 / 个人共享 / 申请审批），
    此处用 filter_accessible_doc_ids 精确校验（Deny Override 铁律）。
    旧数据无权限组标记（cited_doc_ids 空但有引用）：非超管保守跳过，
    避免泄露未知权限视野的历史答案。
    """
    if obj.cited_doc_ids:
        if not (user and getattr(user, 'is_authenticated', False)):
            return False  # 匿名无权限上下文，仅可命中纯知识缓存
        accessible_ids = filter_accessible_doc_ids(user, obj.cited_doc_ids)
        return set(obj.cited_doc_ids) <= set(accessible_ids)
    if obj.citations:
        # 旧数据无权限组标记：无法确认答案权限视野，非超管保守跳过
        return bool(user and getattr(user, 'is_super_admin', False))
    return True


def _try_cache(question: str, root_type: str, user) -> dict:
    """热点缓存命中（组织分组 + 文档兜底）

    权限感知共享缓存策略：
    1. 缓存按答案引用文档的组织归属分组（visibility_scope='public' / 'org_...'），
       不同权限组各自独立一条，互不覆盖（避免权限异构场景下缓存抖动互相污染）；
    2. 命中校验两层：
       a. 权限组粗筛：'public' 零校验；'org_...' 需用户可见组织覆盖（AND 全覆盖）；
       b. 文档级兜底：filter_accessible_doc_ids 对引用文档全通过，
          覆盖黑名单 / 个人共享 / 申请审批等组织维度看不见的权限；
    3. 按 public → 其他分组的顺序尝试，任一通过即命中，全不命中返回 None（重新生成）。
    """
    qh = _hash(question)
    now = timezone.now()
    records = list(HotQaCache.objects.filter(question_hash=qh, root_type=root_type))
    # public 组零校验优先；其余按创建先后（各组独立，互不覆盖）
    records.sort(key=lambda o: (0 if o.visibility_scope == 'public' else 1, o.created_at))
    for obj in records:
        if obj.expires_at and obj.expires_at < now:
            continue
        if not _cache_scope_accessible(user, obj):
            continue
        if not _cache_docs_accessible(user, obj):
            continue
        HotQaCache.objects.filter(id=obj.id).update(hit_count=F('hit_count') + 1, last_hit_at=timezone.now())
        return {'answer': obj.answer, 'citations': obj.citations}
    return None


def _should_update_cache(answer_type: str, is_filtered: bool) -> bool:
    """是否写入热点缓存（agent / general / rag 成功回答均可复用）

    general 无工具调用也需缓存：否则同样问题每次都完整 LLM 生成（数秒延迟）。
    refused（拒答/审查拦截）不缓存：词库会更新，且违规内容不应被缓存复用。
    """
    return answer_type in ('agent', 'general', 'rag') and not is_filtered


def _extract_cited_doc_ids(citations: list) -> list:
    """从 citations 提取引用文档 ID 集合（作为缓存条目的权限组标记）

    权限组 = 答案引用的文档集合：命中缓存时对当前用户按该集合校验权限
    （全部可访问才可命中）。每个 citation 取第一个 chunk 批量反查文档，
    避免逐条查询。
    """
    chunk_ids = []
    for cite in citations or []:
        ids = cite.get('chunk_ids') or []
        if ids:
            chunk_ids.append(ids[0])
    if not chunk_ids:
        return []
    from apps.knowledge.models import DocumentChunk
    return list(DocumentChunk.objects.filter(id__in=chunk_ids)
                .values_list('document_id', flat=True).distinct())


def _update_cache(question: str, root_type: str, user, answer: str, citations: list):
    qh = _hash(question)
    try:
        # 权限组：按引用文档的组织归属分组（public / org_...），命中时按组校验
        cited_doc_ids = _extract_cited_doc_ids(citations)
        org_scope = _build_org_scope(cited_doc_ids)
        obj, created = HotQaCache.objects.get_or_create(
            question_hash=qh, root_type=root_type, visibility_scope=org_scope,
            defaults={
                'question': question[:1000],
                'answer': answer,
                'citations': citations,
                'cited_doc_ids': cited_doc_ids,
                'hit_count': 1,
            }
        )
        if not created:
            # 更新答案与引用，保留 hit_count 累计（update_or_create 每次重置为 1 会失真）
            obj.question = question[:1000]
            obj.answer = answer
            obj.citations = citations
            obj.cited_doc_ids = cited_doc_ids
            obj.save(update_fields=['question', 'answer', 'citations', 'cited_doc_ids', 'last_hit_at'])
    except Exception:
        logger.exception('cache write failed')


def _persist_qa(*, user, session, question, answer, citations,
                retrieval_hits, retrieval_scores, stats, llm_stats,
                root_type, turn_index, answer_type='rag', is_hit_cache=False,
                is_task_split=False, error_type='', is_success=True,
                is_filtered=False, filter_reason='',
                route_source=None, route_trace=None):
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
        route_source=route_source,
        route_trace=route_trace,
        error_type=error_type,
        is_success=is_success,
        is_filtered=is_filtered,
        filter_reason=filter_reason[:128],
        tokens_per_second=round(tokens_per_second, 2),
        # llm_error 为 None），dict.get 对存在的键返回 None 会违反 NOT NULL 约束
        error_message=(llm_stats.get('error') or ''),
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
                    route_trace=_collect_transform_route_trace(tool_traces) or None,
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
        # 查询改写/分解审计：从工具调用链 meta 收集 transform 追踪信息
        route_trace=_collect_transform_route_trace(tool_traces) or None,
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
    if _should_update_cache(answer_type, is_filtered):
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
