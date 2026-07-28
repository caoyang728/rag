"""
apps.knowledge.access - 文档访问权限判定

权限模型（visible_scope 字符串三档 + 黑名单/白名单/跨团队授权）：
- 列表可见：所有登录用户均可看到文档条目（用于发现与申请权限）
- 读取 read（预览 + 对话检索）：所有者 / 管理员 / 可见性匹配 / 有效授权
- 下载 download：文档 allow_download=True 且 (所有者 / 管理员)
- 分享 share：文档 allow_share=True 且 (所有者 / 管理员)

判定优先级（按顺序，命中即停止）：
1. has_deny_user=True 且用户在 DocDenyUser 表中 → 全部拒绝
2. visible_scope='public' → 放行
3. visible_scope='dept' 且 user.dept_node_id == doc.dept_node_id → 放行
4. visible_scope='team' 且 user.team_node_id == doc.team_node_id → 放行
5. has_cross_team=True 且用户在 DocCrossTeam 中 → 放行
6. has_allow_user=True 且用户在 DocAllowUser 中 → 放行
7. 否则拒绝

owner 始终拥有全部权限。
is_manager（super_admin / kb_admin）始终拥有全部权限。
"""
from django.db import models
from django.utils import timezone

from apps.users.models import DocDenyUser, DocAllowUser, DocCrossTeam
from loguru import logger


def get_user_team_ids(user) -> list:
    """获取用户所有团队的 ID 列表"""
    try:
        from apps.users.models import UserTeam
        return list(UserTeam.objects.filter(user=user).values_list('team_id', flat=True))
    except Exception:
        return []


def get_user_team_node_ids(user) -> list:
    """获取用户所有团队的 KnowledgeNode ID（node_level=3）"""
    try:
        from apps.knowledge.models import KnowledgeNode
        team_ids = get_user_team_ids(user)
        if not team_ids:
            return []
        return list(
            KnowledgeNode.objects.filter(
                node_level=3,
                is_deleted=False,
                ref_id__in=team_ids,
            ).values_list('id', flat=True)
        )
    except Exception:
        return []


def get_user_team_codes(user) -> set:
    """获取用户所有团队的 code 集合"""
    try:
        from apps.users.models import Team
        team_ids = get_user_team_ids(user)
        if not team_ids:
            return set()
        return set(
            Team.objects.filter(
                id__in=team_ids,
                is_deleted=False,
            ).values_list('code', flat=True)
        )
    except Exception:
        return set()


def get_user_team_info(user) -> tuple:
    """获取用户所有团队的 (team_ids, node_ids, team_codes)，一次性获取全部信息"""
    try:
        from apps.users.models import UserTeam, Team
        from apps.knowledge.models import KnowledgeNode
        team_ids = list(UserTeam.objects.filter(user=user).values_list('team_id', flat=True))
        if not team_ids:
            return [], [], set()
        node_ids = list(
            KnowledgeNode.objects.filter(
                node_level=3,
                is_deleted=False,
                ref_id__in=team_ids,
            ).values_list('id', flat=True)
        )
        team_codes = set(
            Team.objects.filter(
                id__in=team_ids,
                is_deleted=False,
            ).values_list('code', flat=True)
        )
        return team_ids, node_ids, team_codes
    except Exception:
        return [], [], set()


def get_user_dept_node_id(user):
    """获取用户部门对应的 KnowledgeNode ID（node_level=2）"""
    if not getattr(user, 'is_authenticated', False) or not getattr(user, 'department', None):
        return None
    try:
        from apps.knowledge.models import KnowledgeNode
        node = KnowledgeNode.objects.filter(
            node_level=2,
            is_deleted=False,
            ref_id=user.department.id,
        ).first()
        return node.id if node else None
    except Exception:
        return None





def get_user_denied_doc_ids(user) -> list:
    """获取用户被 DocDenyUser 拉黑的文档 ID 列表"""
    try:
        from apps.users.models import DocDenyUser
        return list(DocDenyUser.objects.filter(uid=user.id).values_list('doc_id', flat=True))
    except Exception:
        return []


def build_user_context(user):
    """预计算用户的团队/部门/管理员身份，供批量权限判定复用，避免 N+1"""
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    led_team_node_ids = set()
    user_team_ids = []
    user_team_node_ids = []
    user_team_codes = set()
    try:
        user_dept_node_id = get_user_dept_node_id(user)
        user_team_ids, user_team_node_ids, user_team_codes = get_user_team_info(user)
        # 团队组长：两种方式 — 1) Team.leader FK 指定  2) 拥有 team_leader 角色且属于某团队
        from apps.users.models import Team
        from apps.knowledge.models import KnowledgeNode
        led_team_ids = list(
            Team.objects.filter(leader=user, is_deleted=False).values_list('id', flat=True)
        )
        if has_role(user, 'team_leader'):
            for tid in user_team_ids:
                if tid not in led_team_ids:
                    led_team_ids.append(tid)
        if led_team_ids:
            led_team_node_ids = set(
                KnowledgeNode.objects.filter(
                    node_level=3, ref_id__in=led_team_ids, is_deleted=False,
                ).values_list('id', flat=True)
            )
    except Exception as e:
        logger.warning('[build_user_context] failed to load user context: %s', e)
        user_dept_node_id = None
        user_team_ids = []
        user_team_node_ids = []
        user_team_codes = set()
    return {
        'is_manager': (getattr(user, 'is_super_admin', False)
                       or getattr(user, 'is_kb_admin', False)),
        'is_team_leader': bool(led_team_node_ids),
        'led_team_node_ids': led_team_node_ids,
        'user_dept_node_id': user_dept_node_id,
        'user_team_ids': user_team_ids,
        'user_team_node_ids': user_team_node_ids,
        'user_team_codes': user_team_codes,
    }


def has_role(user, role_code):
    """检查用户是否拥有指定角色"""
    try:
        from apps.users.models import UserRole
        return UserRole.objects.filter(user=user, role__code=role_code, is_active=True).exists()
    except Exception:
        return False


def _visibility_allows_read(doc, ctx):
    """可见性是否允许读取（visible_scope 字段）"""
    scope = doc.visible_scope
    if scope == 'public':
        return True
    if scope == 'dept':
        return (ctx.get('user_dept_node_id') is not None
                and ctx['user_dept_node_id'] == doc.dept_node_id)
    if scope == 'team':
        return (doc.team_node_id is not None
                and doc.team_node_id in ctx.get('user_team_node_ids', []))
    return False


def _has_active_grant(user, doc, grants_map=None, user_team_codes=None):
    """是否存在有效的文档级授权（DocAllowUser / DocCrossTeam）
    优化：先检查 doc.has_allow_user / doc.has_cross_team 标志，为 False 时跳过表查询
    user_team_codes: 可选，传入已计算的团队 code 集合，避免重复查询"""
    if grants_map is not None:
        allow_docs = grants_map.get('allow_docs', set())
        cross_docs = grants_map.get('cross_team_docs', set())
        return doc.id in allow_docs or doc.id in cross_docs

    now = timezone.now()

    # DocAllowUser：先检查 has_allow_user 标志
    if doc.has_allow_user and DocAllowUser.objects.filter(
        doc_id=doc.id, uid=user.id,
    ).filter(
        models.Q(expire_time__isnull=True) | models.Q(expire_time__gt=now),
    ).exists():
        return True

    # DocCrossTeam：先检查 has_cross_team 标志
    if doc.has_cross_team:
        if user_team_codes is None:
            user_team_codes = get_user_team_codes(user)
        if user_team_codes:
            return DocCrossTeam.objects.filter(
                doc_id=doc.id, team_code__in=user_team_codes,
            ).filter(
                models.Q(expire_time__isnull=True) | models.Q(expire_time__gt=now),
            ).exists()

    return False


def _is_denied(user, doc, grants_map=None):
    """检查用户是否在文档黑名单中"""
    if not doc.has_deny_user:
        return False
    if grants_map is not None:
        return doc.id in grants_map.get('deny_docs', set())
    return DocDenyUser.objects.filter(doc_id=doc.id, uid=user.id).exists()


def resolve_doc_access(user, doc, ctx=None, grants_map=None):
    """返回用户对文档的访问权限标志 dict"""
    if user is None or not getattr(user, 'is_authenticated', False):
        return {'is_owner': False, 'is_manager': False,
                'can_read': False, 'can_download': False, 'can_share': False}

    if ctx is None:
        ctx = build_user_context(user)

    is_owner = doc.owner_id == user.id
    is_manager = (ctx['is_manager'] if ctx
                  else (getattr(user, 'is_super_admin', False) or getattr(user, 'is_kb_admin', False)))

    # 团队组长：对归属于其团队子树内的文档拥有管理权
    is_team_manager = False
    if not is_owner and not is_manager and ctx:
        is_team_manager = (
            ctx.get('is_team_leader', False)
            and doc.team_node_id is not None
            and doc.team_node_id in ctx.get('led_team_node_ids', set())
        )

    effective_manager = is_manager or is_team_manager

    # 所有者 / 管理员 / 团队组长对归属文档始终有全部权限
    if is_owner or effective_manager:
        return {
            'is_owner': is_owner,
            'is_manager': effective_manager,
            'can_read': True,
            'can_download': True,
            'can_share': True,
        }

    # 1. 黑名单检查（最高优先级）
    if _is_denied(user, doc, grants_map):
        return {
            'is_owner': False,
            'is_manager': False,
            'can_read': False,
            'can_download': False,
            'can_share': False,
        }

    # 2~6. 可见性 → 跨团队 → 白名单 依次检查
    can_read = False
    if ctx:
        if _visibility_allows_read(doc, ctx):
            can_read = True
        elif _has_active_grant(user, doc, grants_map):
            can_read = True

    if not can_read:
        return {
            'is_owner': False,
            'is_manager': False,
            'can_read': False,
            'can_download': False,
            'can_share': False,
        }

    # 下载 / 分享：依靠文档自身 allow 标志
    return {
        'is_owner': False,
        'is_manager': False,
        'can_read': True,
        'can_download': bool(doc.allow_download),
        'can_share': bool(doc.allow_share),
    }


def filter_accessible_doc_ids(user, doc_ids):
    """过滤用户有权限访问的文档 ID 列表"""
    if not user or not getattr(user, 'is_authenticated', False) or not doc_ids:
        return []
    from apps.knowledge.models import Document
    user_ctx = build_user_context(user)
    user_team_codes = user_ctx.get('user_team_codes', set()) if user_ctx else get_user_team_codes(user)
    grants_map = build_grants_map(user, doc_ids, user_team_codes=user_team_codes)
    docs = {d.id: d for d in Document.objects.filter(id__in=doc_ids)}
    accessible = []
    for doc_id in doc_ids:
        doc = docs.get(doc_id)
        if doc:
            access = resolve_doc_access(user, doc, ctx=user_ctx, grants_map=grants_map)
            if access.get('can_read', False):
                accessible.append(doc_id)
    return accessible


def build_grants_map(user, doc_ids, user_team_codes=None):
    """批量查询用户对一组文档的有效授权，返回 {
        'allow_docs': set(doc_ids),
        'cross_team_docs': set(doc_ids),
        'deny_docs': set(doc_ids),
    }
    user_team_codes: 可选，传入已计算的团队 code 集合，避免重复查询"""
    if not user or not getattr(user, 'is_authenticated', False) or not doc_ids:
        return {}

    now = timezone.now()
    result = {
        'allow_docs': set(),
        'cross_team_docs': set(),
        'deny_docs': set(),
    }

    # DocAllowUser：uid 匹配，未过期
    allow_rows = DocAllowUser.objects.filter(
        doc_id__in=doc_ids,
        uid=user.id,
    ).filter(
        models.Q(expire_time__isnull=True) | models.Q(expire_time__gt=now),
    ).values_list('doc_id', flat=True)
    result['allow_docs'] = set(allow_rows)

    # DocCrossTeam：user 所属 team_code 匹配，未过期
    if user_team_codes is None:
        user_team_codes = get_user_team_codes(user)
    if user_team_codes:
        cross_rows = DocCrossTeam.objects.filter(
            doc_id__in=doc_ids,
            team_code__in=user_team_codes,
        ).filter(
            models.Q(expire_time__isnull=True) | models.Q(expire_time__gt=now),
        ).values_list('doc_id', flat=True)
        result['cross_team_docs'] = set(cross_rows)

    # DocDenyUser：uid 匹配
    deny_rows = DocDenyUser.objects.filter(
        doc_id__in=doc_ids,
        uid=user.id,
    ).values_list('doc_id', flat=True)
    result['deny_docs'] = set(deny_rows)

    return result
