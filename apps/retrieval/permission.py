"""
权限过滤 - 生成检索 WHERE 子句
根据用户角色/团队/部门，生成 visible_scope 权限 SQL
- visible_scope='public'（旧 visibility=4）: 所有人可见
- visible_scope='team'（旧 visibility=3）: 用户所在团队可见
- visible_scope='dept'（旧 visibility=2）: 用户所在部门可见
+ 超管绕过所有过滤
+ 支持按 root_type 白名单收敛（避免跨库搜索）
"""
from typing import List, Tuple, Optional
from django.db.models import Q


def _get_user_dept_node_id(user) -> Optional[int]:
    """获取用户部门对应的 KnowledgeNode ID"""
    if not user.is_authenticated or not user.department:
        return None
    try:
        from apps.knowledge.models import KnowledgeNode
        node = KnowledgeNode.objects.filter(
            node_level=2,
            is_deleted=False,
            ref_id=user.department.id
        ).first()
        return node.id if node else None
    except Exception:
        return None


def build_permission_q(user, root_types: Optional[List[str]] = None,
                       node_path_prefix: Optional[str] = None,
                       node_ids: Optional[List[int]] = None) -> Q:
    """构造 DocumentVector 查询的权限 Q
    返回：Q(visible_scope='public') | Q(visible_scope='dept', dept_node_id=user.dept_node_id) | Q(visible_scope='team', team_node_id__in=user_team_node_ids)
    """
    # 超管 / kb_admin：直接 True
    if user.is_authenticated and (getattr(user, 'is_super_admin', False)
                                   or getattr(user, 'is_kb_admin', False)):
        q = Q()
    else:
        # 用户团队 node id 列表
        team_node_ids = _get_user_team_node_ids(user) if user.is_authenticated else []
        # 用户部门 node id
        dept_node_id = _get_user_dept_node_id(user) if user.is_authenticated else None
        # 公开
        q = Q(visible_scope='public')
        if user.is_authenticated:
            # 部门可见
            if dept_node_id:
                q = q | Q(visible_scope='dept', dept_node_id=dept_node_id)
            # 团队可见
            if team_node_ids:
                q = q | Q(visible_scope='team', team_node_id__in=team_node_ids)

    # root_type 白名单
    if root_types:
        q = q & Q(root_type__in=root_types)

    # 节点路径前缀
    if node_path_prefix:
        q = q & Q(node_path__startswith=node_path_prefix)

    # 节点 ID 过滤 - 支持节点路径前缀匹配
    if node_ids and node_ids:
        from apps.knowledge.models import KnowledgeNode
        # 获取所有选中节点及其子孙节点的路径前缀
        node_paths = []
        for node_id in node_ids:
            try:
                node = KnowledgeNode.objects.get(id=node_id, is_deleted=False)
                node_paths.append(node.path)
            except KnowledgeNode.DoesNotExist:
                continue
        if node_paths:
            # 构建 OR 查询：node_id 在选中节点中，或者 node_path 以选中节点的路径为前缀
            path_q = Q()
            for path in node_paths:
                path_q = path_q | Q(node_path__startswith=path)
            q = q & (Q(node_id__in=node_ids) | path_q)

    return q


def _get_user_team_node_ids(user) -> List[int]:
    """获取用户所有团队的 KnowledgeNode ID（node_level=3）"""
    try:
        from apps.users.models import UserTeam
        team_ids = list(UserTeam.objects.filter(user=user).values_list('team_id', flat=True))
        if not team_ids:
            return []
        from apps.knowledge.models import KnowledgeNode
        return list(
            KnowledgeNode.objects.filter(
                node_level=3,
                is_deleted=False,
                ref_id__in=team_ids,
            ).values_list('id', flat=True)
        )
    except Exception:
        return []


def build_permission_sql(user, root_types: Optional[List[str]] = None,
                         node_ids: Optional[List[int]] = None) -> Tuple[str, list]:
    """当需要直接写原生 SQL 时（如 pgvector 相似度检索），返回 (where_clause, params)"""
    if user.is_authenticated and (getattr(user, 'is_super_admin', False)
                                   or getattr(user, 'is_kb_admin', False)):
        base = '1=1'
        params: list = []
    else:
        team_node_ids = _get_user_team_node_ids(user) if user.is_authenticated else []
        dept_node_id = _get_user_dept_node_id(user) if user.is_authenticated else None
        conds = ["visible_scope = 'public'"]
        params = []
        if user.is_authenticated:
            if dept_node_id:
                conds.append("(visible_scope = 'dept' AND dept_node_id = %s)")
                params.append(dept_node_id)
            if team_node_ids:
                placeholders = ','.join(['%s'] * len(team_node_ids))
                conds.append(f"(visible_scope = 'team' AND team_node_id IN ({placeholders}))")
                params.extend(team_node_ids)
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
