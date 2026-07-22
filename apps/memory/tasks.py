"""
记忆提炼 Celery 任务
异步提炼，不阻塞主问答流程
- 每 N 轮触发 SessionMemory 提炼
- 每 M 个 Session 触发 UserMemory 提炼
"""
import json
from loguru import logger

from celery import shared_task

from apps.memory.models import Session, SessionMemory, UserMemory
from apps.chat.models import QaRecord



@shared_task(name='memory.refine_session', queue='memory')
def refine_session_memory(session_id: int):
    """提炼会话摘要"""
    from apps.llm.factory import get_llm
    from apps.llm.prompts import SESSION_REFINE_SYSTEM, SESSION_REFINE_USER_TEMPLATE

    try:
        sess = Session.objects.get(id=session_id)
    except Session.DoesNotExist:
        return {'ok': False, 'error': 'session not found'}

    # 取最近 20 轮 QA
    qas = list(QaRecord.objects.filter(session=sess).order_by('-turn_index')[:20])
    if not qas:
        return {'ok': True, 'reason': 'no qa'}
    qas.reverse()
    convo = '\n'.join(f'用户：{q.question}\n助手：{q.answer[:400]}' for q in qas)

    llm = get_llm()
    msgs = [
        {'role': 'system', 'content': SESSION_REFINE_SYSTEM},
        {'role': 'user', 'content': SESSION_REFINE_USER_TEMPLATE.format(conversation=convo)},
    ]
    resp = llm.chat(msgs, temperature=0.2, max_tokens=800)
    try:
        data = json.loads(resp['content'])
    except Exception:
        logger.warning('[refine_session] json parse fail')
        data = {'summary': '（提炼失败）', 'entities': [], 'keywords': []}

    sm, _ = SessionMemory.objects.update_or_create(
        session=sess,
        defaults={
            'summary': data.get('summary', '')[:512],
            'entities': (data.get('entities') or [])[:20],
            'keywords': (data.get('keywords') or [])[:20],
            'turn_refined': sess.turn_count,
        }
    )
    return {'ok': True, 'session_id': sess.id}


@shared_task(name='memory.refine_user', queue='memory')
def refine_user_memory(user_id: int = None):
    """提炼用户画像；user_id=None 则批量刷新所有近期活跃用户"""
    from django.contrib.auth import get_user_model
    from apps.llm.factory import get_llm
    from apps.llm.prompts import USER_REFINE_SYSTEM, USER_REFINE_USER_TEMPLATE

    User = get_user_model()
    if user_id:
        users = User.objects.filter(id=user_id)
    else:
        # 简化：取近 7 天有对话的用户
        from django.utils import timezone
        from datetime import timedelta
        active_uids = QaRecord.objects.filter(
            created_at__gte=timezone.now() - timedelta(days=7)
        ).values_list('user_id', flat=True).distinct()
        users = User.objects.filter(id__in=list(active_uids))

    llm = get_llm()
    updated = 0
    for user in users:
        summaries = list(SessionMemory.objects.filter(
            session__user=user
        ).order_by('-updated_at')[:10].values_list('summary', flat=True))
        if not summaries:
            continue
        joined = '\n'.join(f'- {s}' for s in summaries)
        msgs = [
            {'role': 'system', 'content': USER_REFINE_SYSTEM},
            {'role': 'user',
             'content': USER_REFINE_USER_TEMPLATE.format(count=len(summaries), summaries=joined)},
        ]
        resp = llm.chat(msgs, temperature=0.2, max_tokens=600)
        try:
            data = json.loads(resp['content'])
        except Exception:
            continue
        um, _ = UserMemory.objects.update_or_create(
            user=user,
            defaults={
                'domain_tags': (data.get('domain_tags') or [])[:10],
                'frequent_topics': (data.get('frequent_topics') or [])[:15],
                'preferences': data.get('preferences') or {},
                'profile_text': (data.get('profile_text') or '')[:500],
                'session_refined_count': len(summaries),
            }
        )
        updated += 1
    return {'ok': True, 'updated': updated}
