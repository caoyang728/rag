"""
权限过滤 - 生成检索 WHERE 子句
根据用户角色/团队/部门，生成 visible_scope 权限 SQL
- visible_scope='public'（旧 visibility=4）: 所有人可见
- visible_scope='team'（旧 visibility=3）: 用户所在团队可见
- visible_scope='dept'（旧 visibility=2）: 用户所在部门可见
+ 超管绕过所有过滤
+ DocDenyUser 黑名单过滤（最高优先级）
+ DocAllowUser 个人白名单放行
+ DocCrossTeam 跨团队授权放行
+ 支持按 root_type 白名单收敛（避免跨库搜索）
"""
from typing import List, Tuple, Optional, Dict
from django.db.models import Q

from apps.knowledge.access import get_user_dept_node_id, get_user_denied_doc_ids


def _get_node_filter(node_ids) -> dict:
    """获取节点过滤条件（支持路径前缀匹配子节点）
    返回：{'valid_node_ids': list, 'node_paths': list} 或 None"""
    if not node_ids:
        return None
    from apps.knowledge.models import KnowledgeNode
    result = {'valid_node_ids': [], 'node_paths': []}
    for node in KnowledgeNode.objects.filter(id__in=node_ids, is_deleted=False).values('id', 'path'):
        result['valid_node_ids'].append(node['id'])
        result['node_paths'].append(node['path'])
    return result if result['valid_node_ids'] else None


def _build_permission_conditions(user) -> Dict:
    """生成权限过滤条件的公共数据结构
    返回：{
        'is_manager': bool,
        'team_node_ids': list,
        'dept_node_id': int or None,
        'allowed_doc_ids': list,
        'denied_doc_ids': list,
    }
    """
    result = {
        'is_manager': False,
        'team_node_ids': [],
        'dept_node_id': None,
        'allowed_doc_ids': [],
        'denied_doc_ids': [],
    }

    if not user.is_authenticated:
        return result

    is_manager = getattr(user, 'is_super_admin', False) or getattr(user, 'is_kb_admin', False)
    result['is_manager'] = is_manager

    if is_manager:
        return result

    from apps.knowledge.access import get_user_team_info
    team_ids, team_node_ids, team_codes = get_user_team_info(user)
    result['team_node_ids'] = team_node_ids
    result['dept_node_id'] = get_user_dept_node_id(user)

    from apps.users.models import DocAllowUser, DocCrossTeam
    from django.utils import timezone
    from django.db.models import Q
    now = timezone.now()

    allow_rows = DocAllowUser.objects.filter(
        uid=user.id,
    ).filter(
        Q(expire_time__isnull=True) | Q(expire_time__gt=now),
    ).values_list('doc_id', flat=True)
    result['allowed_doc_ids'] = list(allow_rows)

    if team_codes:
        cross_rows = DocCrossTeam.objects.filter(
            team_code__in=team_codes,
        ).filter(
            Q(expire_time__isnull=True) | Q(expire_time__gt=now),
        ).values_list('doc_id', flat=True)
        result['allowed_doc_ids'].extend(list(cross_rows))

    result['denied_doc_ids'] = get_user_denied_doc_ids(user)

    return result


def build_permission_q(user, root_types: Optional[List[str]] = None,
                       node_path_prefix: Optional[str] = None,
                       node_ids: Optional[List[int]] = None) -> Q:
    """构造 DocumentVector 查询的权限 Q
    返回：(visible_scope权限 OR 白名单文档 OR 所有者文档) AND NOT 黑名单文档
    """
    conds = _build_permission_conditions(user)

    if conds['is_manager']:
        q = Q()
    else:
        q = Q(visible_scope='public')
        if user.is_authenticated:
            q = q | Q(owner_id=user.id)
            if conds['dept_node_id']:
                q = q | Q(visible_scope='dept', dept_node_id=conds['dept_node_id'])
            if conds['team_node_ids']:
                q = q | Q(visible_scope='team', team_node_id__in=conds['team_node_ids'])
            if conds['allowed_doc_ids']:
                q = q | Q(document_id__in=conds['allowed_doc_ids'])

    # root_type 白名单
    if root_types:
        q = q & Q(root_type__in=root_types)

    # 节点路径前缀
    if node_path_prefix:
        q = q & Q(node_path__startswith=node_path_prefix)

    # 节点 ID 过滤 - 支持节点路径前缀匹配
    node_filter = _get_node_filter(node_ids)
    if node_filter:
        path_q = Q()
        for path in node_filter['node_paths']:
            path_q = path_q | Q(node_path__startswith=path)
        q = q & (Q(node_id__in=node_filter['valid_node_ids']) | path_q)

    if user.is_authenticated and conds['denied_doc_ids']:
        q = q & ~Q(document_id__in=conds['denied_doc_ids'])

    return q


def build_permission_sql(user, root_types: Optional[List[str]] = None,
                         node_path_prefix: Optional[str] = None,
                         node_ids: Optional[List[int]] = None) -> Tuple[str, list]:
    """当需要直接写原生 SQL 时（如 pgvector 相似度检索），返回 (where_clause, params)"""
    conds = _build_permission_conditions(user)
    params: list = []

    if conds['is_manager']:
        base = '1=1'
    else:
        sql_conds = ["visible_scope = 'public'"]
        if user.is_authenticated:
            sql_conds.append("owner_id = %s")
            params.append(user.id)
            if conds['dept_node_id']:
                sql_conds.append("(visible_scope = 'dept' AND dept_node_id = %s)")
                params.append(conds['dept_node_id'])
            if conds['team_node_ids']:
                placeholders = ','.join(['%s'] * len(conds['team_node_ids']))
                sql_conds.append(f"(visible_scope = 'team' AND team_node_id IN ({placeholders}))")
                params.extend(conds['team_node_ids'])
            if conds['allowed_doc_ids']:
                placeholders = ','.join(['%s'] * len(conds['allowed_doc_ids']))
                sql_conds.append(f"document_id IN ({placeholders})")
                params.extend(conds['allowed_doc_ids'])
        base = '(' + ' OR '.join(sql_conds) + ')'

    if root_types:
        placeholders = ','.join(['%s'] * len(root_types))
        base = f'({base}) AND root_type IN ({placeholders})'
        params.extend(root_types)

    if node_path_prefix:
        base = f'({base}) AND node_path LIKE %s'
        params.append(node_path_prefix + '%')

    node_filter = _get_node_filter(node_ids)
    if node_filter:
        node_id_placeholders = ','.join(['%s'] * len(node_filter['valid_node_ids']))
        path_conds = []
        for path in node_filter['node_paths']:
            params.append(path)
            path_conds.append(f"node_path LIKE %s || '%'")
        path_str = ' OR '.join(path_conds)
        base = f'({base}) AND (node_id IN ({node_id_placeholders}) OR {path_str})'
        params.extend(node_filter['valid_node_ids'])
    elif node_ids:
        placeholders = ','.join(['%s'] * len(node_ids))
        base = f'({base}) AND node_id IN ({placeholders})'
        params.extend(node_ids)

    if user.is_authenticated and conds['denied_doc_ids']:
        placeholders = ','.join(['%s'] * len(conds['denied_doc_ids']))
        base = f'({base}) AND document_id NOT IN ({placeholders})'
        params.extend(conds['denied_doc_ids'])

    return base, params
