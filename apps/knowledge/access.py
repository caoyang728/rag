"""
apps.knowledge.access - 文档访问权限判定（单文档鉴权）

- visibility_level 三档：TEAM_ONLY / DEPT_ONLY / PUBLIC
- 统一共享表 ResourceShare（文档级 + 节点级继承）
- 黑名单 ResourceBlockList（仅个人，Deny Override 铁律，节点级继承）
- 代码判定基于 permission_key（清除角色硬编码），super_admin 为系统级快路径

判定优先级（Deny Override 铁律）：
0. super_admin → 全权限（系统级快路径，绕过黑名单）
1. Owner → 全权限（Owner 对自己文档全权，不被自己文档拉黑）
2. 黑名单（ResourceBlockList，文档级 + 节点级继承）命中 → 拒绝
3. 管理员（kb_admin / 团队组长对该文档归属团队）→ 全权限
4. 自然可见范围（visibility_level + 组织归属）→ can_read
5. 跨范围共享（ResourceShare，文档级 + 节点级继承）→ can_read
6. 兜底：不命中 = 拒绝

节点级继承（通过 KnowledgeNode.path 前缀匹配）：
- 共享节点 path='/1/5/12/'，则 doc.node.path 以 '/1/5/12/' 开头的文档自动获得共享
- 拉黑节点 path='/1/5/'，则 doc.node.path 以 '/1/5/' 开头的文档全部拒绝
"""
from django.db import models
from django.utils import timezone

from apps.users.models import (
    has_permission, get_user_managed_depts, get_user_managed_teams,
    get_user_dept_ancestors,
)
from apps.knowledge.models import (
    ResourceShare, ResourceBlockList, KnowledgeNode,
    VisibilityLevel, ResourceType, ShareScopeType, ShareStatus,
)


# ============================================================================
# 辅助：有效共享/黑名单过滤条件
# ============================================================================

def _active_q():
    """构造"有效共享/黑名单"过滤条件：status=ACTIVE 且在有效期内

    effective_from NULL = 立即生效；expires_at NULL = 永久有效。
    """
    now = timezone.now()
    return (
        models.Q(status=ShareStatus.ACTIVE)
        & (models.Q(effective_from__isnull=True) | models.Q(effective_from__lte=now))
        & (models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now))
    )


def _build_share_scope_q(user, visible_depts: set, visible_teams: set) -> models.Q:
    """构造"共享给当前用户"的 ResourceShare 过滤条件（个人/团队/部门 OR 关系）"""
    scope_q = models.Q(share_scope_type=ShareScopeType.USER, share_scope_id=user.id)
    if visible_teams:
        scope_q |= models.Q(share_scope_type=ShareScopeType.TEAM, share_scope_id__in=visible_teams)
    if visible_depts:
        scope_q |= models.Q(share_scope_type=ShareScopeType.DEPT, share_scope_id__in=visible_depts)
    return scope_q


# ============================================================================
# 用户上下文预计算（批量权限判定复用，避免 N+1）
# ============================================================================

def build_user_context(user):
    """预计算用户的可见部门/团队、管理员身份，供批量权限判定复用

    返回 dict：
    - is_manager：是否有 kb.manage_all 权限（kb_admin）
    - is_team_leader：是否管理任何团队（managed_team_ids 非空）
    - managed_team_ids：管理的团队 ID 集合（含本团队 + UserTeamScopeRel 授权团队）
    - managed_dept_ids：管理的部门 ID 集合（含本部门 + UserDeptScopeRel 授权部门）
    - visible_depts：可见部门集合（含祖先链，用于 DEPT_ONLY 自然可见范围）
    - visible_teams：可见团队集合（= managed_team_ids，用于 TEAM_ONLY 自然可见范围）

    TODO[缓存层]：配合 L2/L3 缓存使用。
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return None

    managed_depts = get_user_managed_depts(user)
    managed_teams = get_user_managed_teams(user)

    # 可见部门 = 主部门祖先链 ∪ 团队部门祖先链 ∪ 可管理部门
    # （祖先链：用户能看到上级部门下放的 DEPT_ONLY 文档）
    visible_depts = set()
    if user.department_id:
        visible_depts |= get_user_dept_ancestors(user.department_id)
    if user.team_id:
        team_dept_id = getattr(user.team, 'department_id', None) if user.team else None
        if team_dept_id:
            visible_depts |= get_user_dept_ancestors(team_dept_id)
    visible_depts |= managed_depts

    return {
        'is_manager': user.is_super_admin or has_permission(user, 'kb.manage_all'),
        'is_team_leader': bool(managed_teams),
        'managed_team_ids': managed_teams,
        'managed_dept_ids': managed_depts,
        'visible_depts': visible_depts,
        'visible_teams': managed_teams,
    }


# ============================================================================
# 节点级共享/黑名单 path 预计算
# ============================================================================

def _get_user_shared_node_paths(user, ctx=None) -> list:
    """获取共享给用户的节点 path 列表（节点级共享继承，ALL_DESCENDANTS）

    用于单文档鉴权：doc.node.path 以任一共享节点 path 开头 → 获得共享访问。
    """
    visible_depts = ctx.get('visible_depts', set()) if ctx else set()
    visible_teams = ctx.get('visible_teams', set()) if ctx else set()
    if ctx is None:
        # 未传 ctx 时现场计算（性能较差，建议传 ctx）
        visible_depts = _get_user_visible_depts_standalone(user)
        visible_teams = get_user_managed_teams(user)

    shared_node_ids = ResourceShare.objects.filter(
        _active_q(),
        _build_share_scope_q(user, visible_depts, visible_teams),
        resource_type=ResourceType.KNOWLEDGE_NODE,
    ).values_list('resource_id', flat=True)
    if not shared_node_ids:
        return []
    return list(
        KnowledgeNode.objects.filter(id__in=list(shared_node_ids))
        .values_list('path', flat=True)
    )


def _get_user_blocked_node_paths(user) -> list:
    """获取拉黑用户的节点 path 列表（节点级黑名单继承，ALL_DESCENDANTS）

    用于单文档鉴权：doc.node.path 以任一拉黑节点 path 开头 → 拒绝访问。
    """
    blocked_node_ids = ResourceBlockList.objects.filter(
        _active_q(),
        resource_type=ResourceType.KNOWLEDGE_NODE,
        blocked_user=user,
        # 节点级继承仅 ALL_DESCENDANTS 生效（NODE_ONLY 不影响子树）
        block_inherit_mode='ALL_DESCENDANTS',
    ).values_list('resource_id', flat=True)
    if not blocked_node_ids:
        return []
    return list(
        KnowledgeNode.objects.filter(id__in=list(blocked_node_ids))
        .values_list('path', flat=True)
    )


def _get_user_visible_depts_standalone(user) -> set:
    """未传 ctx 时现场计算可见部门集合（与 build_user_context 同逻辑，降级用）"""
    visible = set()
    if user.department_id:
        visible |= get_user_dept_ancestors(user.department_id)
    if user.team_id:
        team_dept_id = getattr(user.team, 'department_id', None) if user.team else None
        if team_dept_id:
            visible |= get_user_dept_ancestors(team_dept_id)
    visible |= get_user_managed_depts(user)
    return visible


# ============================================================================
# 自然可见范围判定
# ============================================================================

def _visibility_allows_read(doc, ctx) -> bool:
    """可见性是否允许读取（visibility_level + 组织归属）

    - PUBLIC：全局全员可见
    - DEPT_ONLY：doc.dept_id 在用户可见部门集合中（含祖先链）
    - TEAM_ONLY：doc.team_id 在用户可见团队集合中
    """
    level = doc.visibility_level
    if level == VisibilityLevel.PUBLIC:
        return True
    if level == VisibilityLevel.DEPT_ONLY:
        return doc.dept_id is not None and doc.dept_id in ctx.get('visible_depts', set())
    if level == VisibilityLevel.TEAM_ONLY:
        return doc.team_id is not None and doc.team_id in ctx.get('visible_teams', set())
    return False


# ============================================================================
# 跨范围共享白名单判定
# ============================================================================

def _has_active_share(user, doc, grants_map=None, ctx=None) -> bool:
    """是否存在有效的跨范围共享（ResourceShare：文档级 + 节点级继承）

    优化：先检查 doc.has_resource_share 标志，为 False 时跳过文档级查询。
    节点级共享通过 doc.node.path 前缀匹配共享节点 path。
    """
    # 1) 文档级共享
    if grants_map is not None:
        if doc.id in grants_map.get('shared_docs', set()):
            return True
    elif doc.has_resource_share:
        visible_depts = ctx.get('visible_depts', set()) if ctx else _get_user_visible_depts_standalone(user)
        visible_teams = ctx.get('visible_teams', set()) if ctx else get_user_managed_teams(user)
        if ResourceShare.objects.filter(
            _active_q(),
            _build_share_scope_q(user, visible_depts, visible_teams),
            resource_type=ResourceType.DOCUMENT,
            resource_id=doc.id,
        ).exists():
            return True

    # 2) 节点级共享继承（doc.node.path 前缀匹配共享节点 path）
    node_path = getattr(doc.node, 'path', '/') if doc.node else '/'
    if node_path and node_path != '/':
        shared_node_paths = _get_user_shared_node_paths(user, ctx)
        for sp in shared_node_paths:
            if sp and node_path.startswith(sp):
                return True
    return False


# ============================================================================
# 黑名单判定（Deny Override 铁律）
# ============================================================================

def _is_denied(user, doc, grants_map=None, ctx=None) -> bool:
    """检查用户是否在文档黑名单中（文档级 + 节点级继承）

    Deny Override：黑名单优先级最高，命中即拒绝（super_admin/Owner 除外，见 resolve_doc_access）。
    优化：先检查 doc.has_block_user 标志，为 False 时跳过文档级查询。
    """
    # 1) 文档级黑名单
    if grants_map is not None:
        if doc.id in grants_map.get('blocked_docs', set()):
            return True
    elif doc.has_block_user:
        if ResourceBlockList.objects.filter(
            _active_q(),
            resource_type=ResourceType.DOCUMENT,
            resource_id=doc.id,
            blocked_user=user,
        ).exists():
            return True

    # 2) 节点级黑名单继承（doc.node.path 前缀匹配拉黑节点 path）
    node_path = getattr(doc.node, 'path', '/') if doc.node else '/'
    if node_path and node_path != '/':
        blocked_node_paths = _get_user_blocked_node_paths(user)
        for bp in blocked_node_paths:
            if bp and node_path.startswith(bp):
                return True
    return False


# ============================================================================
# 核心鉴权：返回用户对文档的访问权限标志 dict
# ============================================================================

def resolve_doc_access(user, doc, ctx=None, grants_map=None) -> dict:
    """返回用户对文档的访问权限标志 dict

    返回：{
        'is_owner': bool,      # 是否 Owner
        'is_manager': bool,    # 是否管理员（kb_admin/团队组长对该文档归属团队）
        'can_read': bool,      # 是否可读（预览 + 对话检索）
        'can_download': bool,  # 是否可下载
        'can_share': bool,     # 是否可分享
    }

    判定优先级（Deny Override 铁律）：
    0. super_admin → 全权限（系统级快路径，绕过黑名单）
    1. Owner → 全权限（Owner 对自己文档全权，不被自己文档拉黑）
    2. 黑名单命中 → 全拒绝
    3. 管理员/团队组长 → 全权限
    4. 自然可见范围 → can_read
    5. 跨范围共享 → can_read
    6. 兜底拒绝
    """
    # 未登录 → 全拒绝
    if user is None or not getattr(user, 'is_authenticated', False):
        return {'is_owner': False, 'is_manager': False,
                'can_read': False, 'can_download': False, 'can_share': False}

    if ctx is None:
        ctx = build_user_context(user)

    is_owner = doc.owner_id == user.id

    # 0) super_admin 系统级快路径（绕过黑名单）
    if user.is_super_admin:
        return {'is_owner': is_owner, 'is_manager': True,
                'can_read': True, 'can_download': True, 'can_share': True}

    # 1) Owner 全权限（绕过黑名单，Owner 不被自己文档拉黑）
    if is_owner:
        return {'is_owner': True, 'is_manager': False,
                'can_read': True, 'can_download': True, 'can_share': True}

    # 2) 黑名单命中 → 全拒绝（Deny Override，最高优先级）
    if _is_denied(user, doc, grants_map, ctx):
        return {'is_owner': False, 'is_manager': False,
                'can_read': False, 'can_download': False, 'can_share': False}

    # 3) 管理员 / 团队组长 → 全权限
    #    kb_admin：有 kb.manage_all 权限
    #    团队组长：doc.team_id 在其管理团队集合中
    is_kb_admin = ctx['is_manager'] if ctx else has_permission(user, 'kb.manage_all')
    is_team_manager = (
        doc.team_id is not None
        and doc.team_id in (ctx.get('managed_team_ids', set()) if ctx else set())
    )
    if is_kb_admin or is_team_manager:
        return {'is_owner': False, 'is_manager': True,
                'can_read': True, 'can_download': True, 'can_share': True}

    # 4) 自然可见范围 → can_read
    can_read = _visibility_allows_read(doc, ctx) if ctx else False

    # 5) 跨范围共享 → can_read
    if not can_read:
        can_read = _has_active_share(user, doc, grants_map, ctx)

    # 6) 兜底拒绝
    if not can_read:
        return {'is_owner': False, 'is_manager': False,
                'can_read': False, 'can_download': False, 'can_share': False}

    # 可读但非管理员：下载/分享依靠文档自身 allow 标志
    return {'is_owner': False, 'is_manager': False,
            'can_read': True,
            'can_download': bool(doc.allow_download),
            'can_share': bool(doc.allow_share)}


# ============================================================================
# 批量鉴权辅助
# ============================================================================

def build_grants_map(user, doc_ids, ctx=None):
    """批量查询用户对一组文档的共享/黑名单，返回 {
        'shared_docs': set(doc_ids),    # 文档级共享白名单
        'blocked_docs': set(doc_ids),   # 文档级黑名单
    }

    注意：节点级共享/黑名单不在此批量查询内（涉及 path 前缀匹配，由 resolve_doc_access 单独判定）。
    ctx：可选，传入已计算的 build_user_context 结果，避免重复查询。
    """
    if not user or not getattr(user, 'is_authenticated', False) or not doc_ids:
        return {'shared_docs': set(), 'blocked_docs': set()}

    visible_depts = ctx.get('visible_depts', set()) if ctx else _get_user_visible_depts_standalone(user)
    visible_teams = ctx.get('visible_teams', set()) if ctx else get_user_managed_teams(user)

    # 文档级共享白名单
    shared_docs = set(
        ResourceShare.objects.filter(
            _active_q(),
            _build_share_scope_q(user, visible_depts, visible_teams),
            resource_type=ResourceType.DOCUMENT,
            resource_id__in=doc_ids,
        ).values_list('resource_id', flat=True)
    )

    # 文档级黑名单
    blocked_docs = set(
        ResourceBlockList.objects.filter(
            _active_q(),
            resource_type=ResourceType.DOCUMENT,
            resource_id__in=doc_ids,
            blocked_user=user,
        ).values_list('resource_id', flat=True)
    )

    return {'shared_docs': shared_docs, 'blocked_docs': blocked_docs}


def filter_accessible_doc_ids(user, doc_ids):
    """过滤用户有 can_read 权限的文档 ID 列表

    流程：
    1. 批量查文档级共享/黑名单（build_grants_map）
    2. 逐文档判定（resolve_doc_access，含节点级共享/黑名单继承）
    """
    if not user or not getattr(user, 'is_authenticated', False) or not doc_ids:
        return []
    from apps.knowledge.models import Document

    user_ctx = build_user_context(user)
    grants_map = build_grants_map(user, doc_ids, ctx=user_ctx)
    docs = {d.id: d for d in Document.objects.filter(id__in=doc_ids).select_related('node')}
    accessible = []
    for doc_id in doc_ids:
        doc = docs.get(doc_id)
        if doc:
            access = resolve_doc_access(user, doc, ctx=user_ctx, grants_map=grants_map)
            if access.get('can_read', False):
                accessible.append(doc_id)
    return accessible
