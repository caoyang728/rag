"""
权限过滤 - 生成检索 WHERE 子句
根据用户角色/团队/所有权，生成 visibility 权限 SQL
- visibility=4 (系统级): 所有人可见
- visibility=3 (公开): 登录用户可见
- visibility=2 (团队): 用户所在团队可见
- visibility=1 (私有): 仅 owner 可见
+ 超管绕过所有过滤
+ 支持按 root_type 白名单收敛（避免跨库搜索）
"""
from typing import List, Tuple, Optional
from django.db.models import Q


def build_permission_q(user, root_types: Optional[List[str]] = None,
                       node_path_prefix: Optional[str] = None,
                       node_ids: Optional[List[int]] = None) -> Q:
    """构造 DocumentVector 查询的权限 Q
    返回：Q(visibility=4) | Q(visibility=3) | Q(visibility=2, owner_team_id__in=user_teams) | Q(owner_id=user.id)
    """
    # 超管：直接 True
    if user.is_authenticated and getattr(user, 'is_super_admin', False):
        q = Q()
    else:
        # 用户团队 id 列表（从 UserTeam）
        team_ids = _get_user_team_ids(user) if user.is_authenticated else []
        # 系统级 + 公开
        q = Q(visibility=4) | Q(visibility=3)
        if user.is_authenticated:
            # owner_id 私有
            q = q | Q(owner_id=user.id)
            # 团队可见
            if team_ids:
                q = q | Q(visibility=2, owner_team_id__in=team_ids)

    # root_type 白名单
    if root_types:
        q = q & Q(root_type__in=root_types)

    # 节点路径前缀
    if node_path_prefix:
        q = q & Q(node_path__startswith=node_path_prefix)

    # 节点 ID 过滤
    if node_ids and node_ids:
        q = q & Q(node_id__in=node_ids)

    return q


def _get_user_team_ids(user) -> List[int]:
    try:
        return list(user.user_teams.values_list('team_id', flat=True))
    except Exception:
        return []


def build_permission_sql(user, root_types: Optional[List[str]] = None,
                         node_ids: Optional[List[int]] = None) -> Tuple[str, list]:
    """当需要直接写原生 SQL 时（如 pgvector 相似度检索），返回 (where_clause, params)"""
    if user.is_authenticated and getattr(user, 'is_super_admin', False):
        base = '1=1'
        params: list = []
    else:
        team_ids = _get_user_team_ids(user) if user.is_authenticated else []
        conds = ['visibility = 4', 'visibility = 3']
        params = []
        if user.is_authenticated:
            conds.append('owner_id = %s')
            params.append(user.id)
            if team_ids:
                placeholders = ','.join(['%s'] * len(team_ids))
                conds.append(f'(visibility = 2 AND owner_team_id IN ({placeholders}))')
                params.extend(team_ids)
        base = '(' + ' OR '.join(conds) + ')'

    if root_types:
        placeholders = ','.join(['%s'] * len(root_types))
        base = f'({base}) AND root_type IN ({placeholders})'
        params.extend(root_types)

    if node_ids:
        placeholders = ','.join(['%s'] * len(node_ids))
        base = f'({base}) AND node_id IN ({placeholders})'
        params.extend(node_ids)

    return base, params
