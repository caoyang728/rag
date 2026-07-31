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
        do_rerank: bool = True) -> dict:
    """一次完整问答
    返回 QaRecord 数据 + 检索命中"""
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

    # 2. 任务拆分判断
    if do_task_split:
        split = maybe_split(question)
        if split.get('need_split'):
            return execute_split(user, session, question, split, root_types=root_types)

    # 3. 混合检索
    try:
        retrieval = hybrid_search(question, user, root_types=root_types, node_ids=node_ids, do_rerank=do_rerank)
        chunks = retrieval['chunks']
        r_stats = retrieval['stats']
    except EmbeddingException as e:
        logger.error('[Executor] embedding failed during search: %s', e)
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
            logger.info('[Executor] permission filter removed %d chunks', len(chunks) - len(filtered_chunks))
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


def ask_stream(user, question: str, session: Session,
               root_types: list = None,
               node_ids: list = None,
               use_cache: bool = True,
               do_task_split: bool = False,
               do_rerank: bool = True):
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

    # 1. 热点缓存命中：一次性输出完整答案，ttfb 即缓存命中耗时
    if use_cache:
        cached = _try_cache(question, root_type, user)
        if cached:
            ttfb_ms = int((time.time() - t0) * 1000)
            yield {
                'type': 'start',
                'session_id': session.id,
                'citations': cached.get('citations', []),
                'is_hit_cache': True,
            }
            yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
            yield {'type': 'delta', 'delta': cached['answer']}

            qa = _persist_qa(
                user=user, session=session, question=question, answer=cached['answer'],
                citations=cached.get('citations', []),
                retrieval_hits=[], retrieval_scores=[],
                stats={'latency_total_ms': int((time.time() - t0) * 1000),
                       'latency_ttfb_ms': ttfb_ms},
                llm_stats={}, root_type=root_type, turn_index=turn_index,
                answer_type='rag', is_hit_cache=True,
            )
            MemoryManager().append_turn(session, question, cached['answer'])

            yield {
                'type': 'done',
                'message_id': qa.id,
                'session_id': session.id,
                'citations': cached.get('citations', []),
                'stats': {
                    'total_ms': int((time.time() - t0) * 1000),
                    'ttfb_ms': ttfb_ms,
                    'is_hit_cache': True,
                },
            }
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
            yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
            yield {'type': 'delta', 'delta': result.get('answer', '')}
            yield {
                'type': 'done',
                'message_id': result.get('qa_id'),
                'session_id': session.id,
                'citations': result.get('citations', []),
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
        logger.error('[ask_stream] embedding failed during search: %s', e)
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
            logger.info('[ask_stream] permission filter removed %d chunks', len(chunks) - len(filtered_chunks))
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
    if not chunks:
        # 无相关片段：直接吐拒答文案
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
        try:
            for chunk in llm.stream(messages, temperature=0.3, max_tokens=2048):
                # finish 帧：Provider 会在最后发一帧 finish=True，可能带 error
                if chunk.get('finish'):
                    if chunk.get('error'):
                        llm_error = chunk['error']
                    break
                delta = chunk.get('delta', '')
                if delta:
                    # 首个 delta 触发 first_token 事件（仅一次）
                    if ttfb_ms is None:
                        ttfb_ms = int((time.time() - t0) * 1000)
                        yield {'type': 'first_token', 'ttfb_ms': ttfb_ms}
                    full_answer.append(delta)
                    yield {'type': 'delta', 'delta': delta}
        except GeneratorExit:
            # 客户端主动终止流式：保存已生成的部分回答到 QaRecord（不能 yield，连接已断）
            logger.info('[ask_stream] client aborted, saving partial answer (%d chars)', len(full_answer))
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
    )

    # 9. 记录短时记忆 + 更新热点缓存
    mm.append_turn(session, question, answer)
    if answer_type == 'rag':
        _update_cache(question, root_type, user, answer, citations)

    # 10. 发送 done 事件
    yield {
        'type': 'done',
        'message_id': qa.id,
        'session_id': session.id,
        'citations': citations,
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
                is_task_split=False, error_type='', is_success=True):
    """持久化问答记录 + 实时指标上报
    - error_type / is_success / tokens_per_second 字段填充
      error_type 分类记录 LLM/Embedding 错误原因，便于统计细分指标
      is_success=False 表示链路中断（LLM 错误、Embedding 失败等），
      区别于 answer_type='refused'（正常的"无相关资料"拒答）
      tokens_per_second 在保存时计算，避免 Dashboard 端重复计算
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

    return qa
