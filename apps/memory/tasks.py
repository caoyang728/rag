"""
记忆提炼 Celery 任务
异步提炼，不阻塞主问答流程
- 每 N 轮触发 SessionMemory 提炼
- 每 M 个 Session 触发 UserMemory 提炼
"""
from loguru import logger

from celery import shared_task

from apps.memory.models import Session, SessionMemory, UserMemory
from apps.memory.parser import (
    SessionRefineSchema, UserRefineSchema, llm_with_retry,
)
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

    result = llm_with_retry(llm, msgs, SessionRefineSchema, max_retries=3, temperature=0.2, max_tokens=800)
    
    if result:
        data = {
            'summary': result.summary,
            'entities': result.entities,
            'keywords': result.keywords,
        }
    else:
        logger.warning(f'[refine_session] All retries failed for session {session_id}')
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
    """增量提炼用户画像：每日凌晨提炼上一日有新增对话的用户，已有画像仅作参考"""
    from django.contrib.auth import get_user_model
    from django.utils import timezone
    from datetime import timedelta
    from apps.llm.factory import get_llm
    from apps.llm.prompts import (
        USER_REFINE_SYSTEM, USER_REFINE_USER_TEMPLATE,
        USER_REFINE_INCREMENTAL_SYSTEM, USER_REFINE_INCREMENTAL_USER_TEMPLATE,
    )

    User = get_user_model()
    today = timezone.now().date()
    yesterday_start = timezone.make_aware(timezone.datetime(today.year, today.month, today.day)) - timedelta(days=1)
    yesterday_end = timezone.make_aware(timezone.datetime(today.year, today.month, today.day))

    if user_id:
        users = User.objects.filter(id=user_id)
    else:
        active_uids = QaRecord.objects.filter(
            created_at__gte=yesterday_start,
            created_at__lt=yesterday_end,
            user__isnull=False,
        ).values_list('user_id', flat=True).distinct()
        users = User.objects.filter(id__in=list(active_uids))

    llm = get_llm()
    updated = 0

    user_ids = [u.id for u in users]
    qas_by_user = {}
    for qa in QaRecord.objects.filter(
        user_id__in=user_ids,
        created_at__gte=yesterday_start,
        created_at__lt=yesterday_end,
    ).order_by('user_id', 'created_at'):
        qas_by_user.setdefault(qa.user_id, []).append(qa)

    for user in users:
        um, _ = UserMemory.objects.get_or_create(user=user)

        yesterday_qas = qas_by_user.get(user.id, [])

        if not yesterday_qas:
            continue

        new_conversations = '\n'.join(
            f'用户：{q.question}\n助手：{q.answer[:400]}' for q in yesterday_qas
        )

        existing_profile = um.profile_text or ''

        msgs = [
            {'role': 'system', 'content': USER_REFINE_INCREMENTAL_SYSTEM},
            {'role': 'user',
             'content': USER_REFINE_INCREMENTAL_USER_TEMPLATE.format(
                 existing_profile=existing_profile,
                 new_conversations=new_conversations,
             )},
        ]

        result = llm_with_retry(llm, msgs, UserRefineSchema, max_retries=3, temperature=0.2, max_tokens=600)
        
        if result:
            um.domain_tags = result.domain_tags[:10]
            um.frequent_topics = result.frequent_topics[:15]
            um.preferences = result.preferences or {}
            um.profile_text = result.profile_text[:500]
            um.session_refined_count = (um.session_refined_count or 0) + 1
            um.save()
            updated += 1
        else:
            logger.warning(f'[refine_user] All retries failed for user {user.id}, keeping existing profile')
    return {'ok': True, 'updated': updated}
