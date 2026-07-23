"""
Agent Executor - 问答主流程编排
- 完整链路：热点缓存 → 任务拆分判断 → 混合检索 → 记忆加载 → LLM 生成 → 记录 QA → 更新缓存
- 全链路耗时/Token/成本记录
- 拒答机制：无相关片段时降级为 general_reasoning 或 "无相关资料"
"""
import hashlib
import json
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



def _normalize(q: str) -> str:
    return ''.join(q.strip().lower().split())


def _hash(q: str) -> str:
    return hashlib.sha256(_normalize(q).encode('utf-8')).hexdigest()


def _visibility_scope(user) -> str:
    if not user or not user.is_authenticated:
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
        )
        
        return {
            'qa_id': qa.id,
            'answer': answer,
            'citations': citations,
            'chunks': [],
            'is_hit_cache': False,
            'stats': {'total_ms': int((time.time() - t0) * 1000), 'error': str(e)},
        }

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
                scene='qa', user=user if user.is_authenticated else None,
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

    # 6. 组装引用
    citations = [
        {'index': i + 1, 'chunk_id': c['chunk_id'],
         'doc_title': c.get('doc_title', ''), 'section': c.get('section_path', ''),
         'page': c.get('page_number')}
        for i, c in enumerate(chunks)
    ]

    # 7. 落 QA 记录
    total_ms = int((time.time() - t0) * 1000)
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


def _try_cache(question: str, root_type: str, user) -> dict:
    scope = 'private' if user and user.is_authenticated else 'public'
    qh = _hash(question)
    now = timezone.now()
    obj = HotQaCache.objects.filter(
        question_hash=qh, root_type=root_type,
        visibility_scope__in=[scope, 'public'],
    ).first()
    if not obj:
        return None
    if obj.expires_at and obj.expires_at < now:
        return None
    HotQaCache.objects.filter(id=obj.id).update(hit_count=F('hit_count') + 1, last_hit_at=timezone.now())
    return {'answer': obj.answer, 'citations': obj.citations}


def _update_cache(question: str, root_type: str, user, answer: str, citations: list):
    scope = _visibility_scope(user)
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
                is_task_split=False):
    qa = QaRecord.objects.create(
        session=session,
        user=user if user.is_authenticated else None,
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
        tokens_prompt=llm_stats.get('tokens_prompt', 0),
        tokens_completion=llm_stats.get('tokens_completion', 0),
        cost_estimate=Decimal(str(llm_stats.get('cost', 0))),
        llm_provider=llm_stats.get('llm_provider', 'deepseek'),
        llm_model=llm_stats.get('llm_model', 'deepseek-chat'),
        is_hit_cache=is_hit_cache,
        is_task_split=is_task_split,
    )
    return qa
