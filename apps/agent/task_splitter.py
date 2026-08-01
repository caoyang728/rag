"""
复杂任务拆分器
- LLM 输出结构化 JSON（need_split + sub_tasks）
- 有依赖关系的子任务串行，无依赖的并行
- 每个子任务独立检索+回答，最后合并
"""
import json
from loguru import logger
from typing import Dict, List, Any
from concurrent.futures import ThreadPoolExecutor

from apps.llm.factory import get_llm
from apps.llm.prompts import (
    TASK_SPLIT_SYSTEM, TASK_SPLIT_USER_TEMPLATE,
    TASK_MERGE_SYSTEM, TASK_MERGE_USER_TEMPLATE,
)



def maybe_split(question: str) -> Dict[str, Any]:
    """让 LLM 判断是否需要拆分"""
    llm = get_llm()
    msgs = [
        {'role': 'system', 'content': TASK_SPLIT_SYSTEM},
        {'role': 'user', 'content': TASK_SPLIT_USER_TEMPLATE.format(question=question)},
    ]
    resp = llm.chat(msgs, temperature=0.0, max_tokens=800)
    raw = (resp.get('content') or '').strip()
    # 兼容 ```json 包裹
    if raw.startswith('```'):
        raw = raw.strip('`').split('\n', 1)[-1]
        if raw.endswith('```'):
            raw = raw[:-3]
    try:
        data = json.loads(raw)
    except Exception:
        logger.warning('[TaskSplit] json parse fail: %s', raw[:200])
        return {'need_split': False, 'reason': 'llm output invalid json'}
    return data


def execute_split(user, session, question: str, split: Dict[str, Any],
                  root_types: list = None) -> Dict[str, Any]:
    """执行子任务并合并"""
    sub_tasks: List[Dict[str, Any]] = split.get('sub_tasks', [])
    if not sub_tasks:
        return {'answer': '（任务拆分为空）', 'chunks': [], 'is_hit_cache': False, 'qa_id': None}

    # 按依赖分层
    answers: Dict[int, str] = {}
    remaining = list(sub_tasks)
    while remaining:
        # 找当前无未完成依赖的任务
        current_batch = [
            t for t in remaining
            if all(dep in answers for dep in t.get('depends_on', []))
        ]
        if not current_batch:
            logger.warning('[TaskSplit] 循环依赖或未满足，剩余 %s', remaining)
            break

        def _run(t):
            # 简化：不落 QaRecord，仅调用 RAG
            from apps.retrieval.hybrid import hybrid_search
            from apps.llm.prompts import build_qa_messages
            r = hybrid_search(t['question'], user, root_types=root_types, do_rerank=True)
            chunks = r['chunks']
            llm = get_llm()
            msgs = build_qa_messages(t['question'], chunks)
            resp = llm.chat(msgs, temperature=0.3, max_tokens=1024)
            return t['index'], resp.get('content', ''), chunks

        with ThreadPoolExecutor(max_workers=min(4, len(current_batch))) as pool:
            for idx, ans, _chunks in pool.map(_run, current_batch):
                answers[idx] = ans

        for t in current_batch:
            remaining.remove(t)

    # 合并
    llm = get_llm()
    sub_str = '\n\n'.join(
        f'子问题 {t["index"]}: {t["question"]}\n答案：{answers.get(t["index"], "")}'
        for t in sub_tasks
    )
    merge_msgs = [
        {'role': 'system', 'content': TASK_MERGE_SYSTEM},
        {'role': 'user',
         'content': TASK_MERGE_USER_TEMPLATE.format(question=question, sub_answers=sub_str)},
    ]
    merged = llm.chat(merge_msgs, temperature=0.3, max_tokens=2048)
    final_answer = merged.get('content', '')

    # 落 QaRecord
    from apps.agent.executor import _persist_qa
    qa = _persist_qa(
        user=user, session=session, question=question, answer=final_answer,
        citations=[], retrieval_hits=[], retrieval_scores=[],
        stats={'latency_total_ms': 0}, llm_stats={
            'llm_provider': merged.get('provider', 'deepseek'),
            'llm_model': merged.get('model', 'deepseek-chat'),
            'tokens_prompt': merged.get('prompt_tokens', 0),
            'tokens_completion': merged.get('completion_tokens', 0),
            'cost': merged.get('cost', 0),
            'latency_llm_ms': merged.get('latency_ms', 0),
        },
        root_type=root_types[0] if root_types else 'company_doc',
        turn_index=(session.turn_count or 0) + 1,
        is_task_split=True,
    )
    return {'qa_id': qa.id, 'answer': final_answer, 'citations': [],
            'chunks': [], 'is_hit_cache': False,
            'sub_tasks': sub_tasks, 'sub_answers': answers,
            'stats': {'total_ms': 0}}
