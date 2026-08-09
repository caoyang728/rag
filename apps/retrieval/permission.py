"""
retrieval app - 检索层权限过滤

核心函数 build_permission_q：构造 DocumentVector 的权限过滤 Q 对象
一次 SQL 完成"自然可见范围 + Owner + 跨范围共享 + 黑名单剔除"，避免 N+1 查询。

判定优先级（Deny Override 铁律）：
  0. 黑名单（ResourceBlockList）命中 → 立即剔除，不再执行任何白名单判定（对超管也生效）
  1. 系统级管理员（super_admin）→ 全部可见（但不绕过黑名单）
  2. 自然可见范围（visibility_level + 组织归属）
  3. 资源所有权（Owner 直接可见，Owner 绕过黑名单）
  4. 跨范围共享白名单（ResourceShare：文档级 + 节点级继承）
  5. 兜底：不命中 = 不召回

节点级共享继承（resource_type=KNOWLEDGE_NODE + inherit_mode=ALL_DESCENDANTS）：
- 共享给某节点 = 该节点 + 所有后代节点 + 后代节点下所有文档自动可见
- 通过 DocumentVector.node_path 前缀匹配（startswith）一次搞定，无需递归 CTE

黑名单过滤策略：
- 文档级黑名单：检索时用 NOT IN 子查询排除（has_block_user 标志位跳过空子查询）
- 节点级黑名单：留 access.py 二次过滤（涉及 path 前缀匹配，召回后过滤可接受）
"""
from django.db.models import Q
from django.utils import timezone

from apps.knowledge.models import (
    VisibilityLevel, ResourceShare, ResourceBlockList,
    ShareStatus, ResourceType, ShareScopeType, KnowledgeNode,
)
from apps.users.models import (
    get_user_managed_depts, get_user_managed_teams, get_user_dept_ancestors,
)


def _active_share_q():
    """构造"有效共享/黑名单"过滤条件：status=ACTIVE 且在有效期内

    effective_from NULL = 立即生效；expires_at NULL = 永久有效。
    与 users.models._active_grant_filter 语义一致，但作用于 ResourceShare/ResourceBlockList。
    """
    now = timezone.now()
    return (
        Q(status=ShareStatus.ACTIVE)
        & (Q(effective_from__isnull=True) | Q(effective_from__lte=now))
        & (Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    )


def _get_user_visible_depts(user) -> set:
    """获取用户可见部门集合 —— 用于 DEPT_ONLY 文档过滤

    组合来源：
    1. 用户主部门的祖先链（含自身）：用户能看到上级部门下放的 DEPT_ONLY 文档
    2. 用户团队所属部门的祖先链（含自身）：通过团队归属看到部门文档
    3. 用户可管理部门（UserDeptScopeRel 授权）：管理者视角

    TODO[缓存层]：配合 L2 缓存（perm:scope:dept:{uid}）使用。
    """
    visible = set()
    # 1) 主部门祖先链
    if user.department_id:
        visible |= get_user_dept_ancestors(user.department_id)
    # 2) 团队所属部门祖先链（若有团队）
    if user.team_id:
        team_dept_id = getattr(user.team, 'department_id', None) if user.team else None
        if team_dept_id:
            visible |= get_user_dept_ancestors(team_dept_id)
    # 3) 可管理部门（管理者）
    visible |= get_user_managed_depts(user)
    return visible


def _get_user_visible_teams(user) -> set:
    """获取用户可见团队集合 —— 用于 TEAM_ONLY 文档过滤

    来源：get_user_managed_teams（本团队 + UserTeamScopeRel 授权团队）
    TODO[缓存层]：配合 L3 缓存（perm:scope:team:{uid}）使用。
    """
    return get_user_managed_teams(user)


def _build_share_scope_q(user, visible_depts: set, visible_teams: set) -> Q:
    """构造"共享给当前用户"的 ResourceShare 过滤条件

    匹配维度（OR 关系）：
    - 共享给个人（share_scope_type=USER, share_scope_id=user.id）
    - 共享给用户所在团队（share_scope_type=TEAM, share_scope_id IN visible_teams）
    - 共享给用户可见部门（share_scope_type=DEPT, share_scope_id IN visible_depts）
    """
    scope_q = Q(share_scope_type=ShareScopeType.USER, share_scope_id=user.id)
    if visible_teams:
        scope_q |= Q(share_scope_type=ShareScopeType.TEAM, share_scope_id__in=visible_teams)
    if visible_depts:
        scope_q |= Q(share_scope_type=ShareScopeType.DEPT, share_scope_id__in=visible_depts)
    return scope_q


def _get_shared_doc_ids(user, visible_depts: set, visible_teams: set):
    """获取共享给用户的文档 ID 子查询（文档级共享）

    返回 ResourceShare 的 resource_id 子查询，用于 DocumentVector.document_id__in。
    """
    return ResourceShare.objects.filter(
        _active_share_q(),
        _build_share_scope_q(user, visible_depts, visible_teams),
        resource_type=ResourceType.DOCUMENT,
    ).values('resource_id')


def _get_shared_node_paths(user, visible_depts: set, visible_teams: set) -> list:
    """获取共享给用户的节点 path 列表（节点级共享继承）

    流程：
    1. 查 ResourceShare 中 resource_type=KNOWLEDGE_NODE 且共享给用户的节点 ID
    2. 查 KnowledgeNode 获取这些节点的 path
    3. 返回 path 列表，用于 DocumentVector.node_path__startswith 前缀匹配

    节点级继承：共享节点 path = '/1/5/12/'，则 node_path LIKE '/1/5/12/%' 的所有文档可见。
    """
    shared_node_ids = ResourceShare.objects.filter(
        _active_share_q(),
        _build_share_scope_q(user, visible_depts, visible_teams),
        resource_type=ResourceType.KNOWLEDGE_NODE,
    ).values_list('resource_id', flat=True)
    if not shared_node_ids:
        return []
    return list(
        KnowledgeNode.objects.filter(id__in=list(shared_node_ids))
        .values_list('path', flat=True)
    )


def _get_blocked_doc_ids(user):
    """获取拉黑当前用户的文档 ID 子查询（文档级黑名单）

    返回 ResourceBlockList 的 resource_id 子查询，用于 NOT IN 排除。
    节点级黑名单（拉黑某节点 = 子树全拒绝）留 access.py 二次过滤。
    """
    return ResourceBlockList.objects.filter(
        _active_share_q(),
        resource_type=ResourceType.DOCUMENT,
        blocked_user=user,
    ).values('resource_id')


def build_permission_q(user, root_types=None, node_path_prefix=None, node_ids=None) -> Q:
    """构造 DocumentVector 权限过滤 Q 对象 —— 检索层核心

    参数：
    - user：当前用户（未登录返回空集 Q）
    - root_types：根节点类型过滤（如 ['kb_default']，可选）
    - node_path_prefix：节点路径前缀过滤（如 '/1/5/'，可选）
    - node_ids：节点 ID 列表过滤（可选）

    返回：Q 对象，应用于 DocumentVector.objects.filter(q)

    判定逻辑（Deny Override）：
    1. 未登录 → 空集
    2. super_admin → 全可见（黑名单由 access.py 二次过滤）
    3. 普通用户：
       - 自然可见范围（PUBLIC / DEPT_ONLY / TEAM_ONLY）
       - OR Owner 直接可见
       - OR 文档级共享白名单
       - OR 节点级共享继承（node_path 前缀匹配）
    4. 黑名单剔除（文档级，has_block_user 标志位优化）
    5. 业务过滤（root_types / node_path_prefix / node_ids）

    性能：高频调用，建议配合缓存层（L5 perm:filter:{uid}:{hash}）使用。
    """
    # 1) 未登录 → 空集（不召回任何文档）
    if user is None or not getattr(user, 'is_authenticated', False):
        return Q(pk__in=[])

    # 2) super_admin → 全可见（黑名单在下方统一剔除，不绕过 Deny Override 铁律）
    if user.is_super_admin:
        q = Q()
    else:
        # 3) 普通用户：计算可见部门/团队集合
        visible_depts = _get_user_visible_depts(user)
        visible_teams = _get_user_visible_teams(user)

        # 3a) 自然可见范围（visibility_level + 组织归属）
        q_natural = (
            Q(visibility_level=VisibilityLevel.PUBLIC)
            | (Q(visibility_level=VisibilityLevel.DEPT_ONLY) & Q(dept_id__in=visible_depts))
            | (Q(visibility_level=VisibilityLevel.TEAM_ONLY) & Q(team_id__in=visible_teams))
        )
        # 3b) Owner 直接可见
        q_owner = Q(owner_id=user.id)
        # 3c) 文档级共享白名单（has_resource_share 标志位跳过空子查询）
        q_doc_share = Q(has_resource_share=True) & Q(
            document_id__in=_get_shared_doc_ids(user, visible_depts, visible_teams)
        )
        # 3d) 节点级共享继承（node_path 前缀匹配共享节点的 path）
        q_node_share = Q()
        shared_node_paths = _get_shared_node_paths(user, visible_depts, visible_teams)
        for path in shared_node_paths:
            if path:
                q_node_share |= Q(node_path__startswith=path)

        q = q_natural | q_owner | q_doc_share | q_node_share

    # 3e) 仅召回活跃版本：非活跃旧版本（被新版本替换）不参与检索，权限判定优先级不变
    q &= Q(is_active=True)

    # 4) 黑名单剔除（文档级，has_block_user 标志位跳过空子查询）
    #    对所有用户生效（含超管，Deny Override 铁律）；Owner 绕过由 access.py 二次过滤保证。
    #    节点级黑名单留 access.py 二次过滤（涉及 path 前缀匹配，SQL 复杂）
    q &= ~Q(has_block_user=True, document_id__in=_get_blocked_doc_ids(user))

    # 5) 业务过滤（root_types / node_path_prefix / node_ids）
    if root_types:
        q &= Q(root_type__in=root_types)
    if node_path_prefix:
        q &= Q(node_path__startswith=node_path_prefix)
    if node_ids:
        q &= Q(node_id__in=node_ids)

    return q
