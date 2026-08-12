"""
Wiki 页面访问权限判定

Wiki 页面挂载在知识节点（node）或图谱社区（community）上，权限来源不同：
- node 页面：可见性对齐该节点下文档的访问判定 —— 用户能读到该节点下任一
  已完成（status='done'）且未删除的文档，即认为可读该节点的 Wiki 页面。
  口径说明：Wiki 是节点下文档的聚合摘要，权限取"节点内容有任一可读"的宽松
  口径（与检索层 build_permission_q 的节点召回语义一致）；节点级共享 / 黑名单
  继承由 apps.knowledge.access.resolve_doc_access 统一判定，此处直接复用，
  避免二次实现权限逻辑导致口径漂移。
- community 页面：图谱社区没有独立的文档权限维度，仅系统管理员 / 知识库
  管理员可读。

管理权限（触发生成 / 刷新 / 标记过期）对齐 DocumentUploadView 的节点上传判定：
- super_admin / kb_admin：全部节点
- dept_manager（有 user.manage 且可管理部门）：本部门节点或其子节点
- team_leader（有 user.manage 且管理团队）/ contributor：本团队节点或其子节点
"""
from apps.knowledge.access import filter_accessible_doc_ids
from apps.knowledge.models import Document
from apps.users.models import (
    has_permission, get_user_managed_depts, get_user_managed_teams,
    Role, UserRoleRel,
)


def get_accessible_node_ids(user, node_ids=None) -> set:
    """获取用户可读的知识节点 ID 集合（节点下存在用户可读的已完成文档）

    Args:
        user: 当前用户
        node_ids: 限定候选节点 ID 列表（可选，用于缩小查询范围）

    Returns:
        set[node_id]
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return set()

    qs = Document.objects.filter(is_deleted=False, status='done')
    if node_ids:
        qs = qs.filter(node_id__in=list(node_ids))
    doc_ids = list(qs.values_list('id', flat=True))
    if not doc_ids:
        return set()

    # 复用文档级权限判定（含黑名单 / 共享 / 可见范围 / 节点级继承）
    accessible_doc_ids = filter_accessible_doc_ids(user, doc_ids)
    if not accessible_doc_ids:
        return set()

    return set(
        Document.objects.filter(id__in=accessible_doc_ids)
        .values_list('node_id', flat=True)
    )


def can_read_wiki(user, page) -> bool:
    """判断用户是否有权浏览 Wiki 页面

    Args:
        user: 当前用户
        page: WikiPage 实例

    Returns:
        True 可读；False 不可读
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False

    # super_admin / kb_admin 拥有绝对浏览权限
    # 对 Wiki 的浏览不受文档级权限约束，与列表页 get_queryset 口径一致
    if getattr(user, 'is_super_admin', False) or getattr(user, 'is_kb_admin', False):
        return True

    # node 页面：节点下存在用户可读的文档即可浏览
    if page.node_id:
        return page.node_id in get_accessible_node_ids(user, [page.node_id])

    # community 页面：无文档权限维度，非管理员不可读（管理员已上方提前返回）
    return False


def _is_contributor(user) -> bool:
    """用户是否被显式授予内容贡献者角色（contributor）

    与文档上传权限口径一致：贡献者可管理本团队节点的 Wiki 生成 / 刷新。
    """
    return Role.objects.filter(
        role_key='contributor',
        id__in=UserRoleRel.objects.filter(
            user=user, status='ACTIVE',
        ).values_list('role_id', flat=True),
    ).exists()


def can_manage_wiki(user, node) -> bool:
    """用户是否有权为该节点触发生成 / 刷新 Wiki

    权限口径对齐 DocumentUploadView._check_node_upload_permission，
    避免"能上传文档但不能生成 Wiki"与"能生成 Wiki 但不能上传文档"的口径割裂。

    Args:
        user: 当前用户
        node: KnowledgeNode 实例

    Returns:
        True 有权限；False 无权限
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False

    # 请求内缓存：列表页同一用户会逐行触发本判定，首次计算后挂到 user 实例，
    # 避免 is_super_admin / 属地授权 / 角色关系在单次请求内重复查库
    perm = getattr(user, '_wiki_perm_cache', None)
    if perm is None:
        perm = {
            'is_admin': getattr(user, 'is_super_admin', False)
                        or getattr(user, 'is_kb_admin', False),
            'managed_depts': get_user_managed_depts(user),
            'managed_teams': get_user_managed_teams(user),
            'can_manage': has_permission(user, 'user.manage'),
            'is_contributor': _is_contributor(user),
        }
        user._wiki_perm_cache = perm

    # 系统管理员 / 文档管理员：全部节点
    if perm['is_admin']:
        return True

    managed_depts = perm['managed_depts']
    managed_teams = perm['managed_teams']
    can_manage = perm['can_manage']

    # 部门经理：节点本身或父节点为该部门节点（部门属地授权）
    if can_manage and managed_depts:
        if (node.node_level == 2 and node.ref_id in managed_depts) or \
           (node.parent and node.parent.node_level == 2 and node.parent.ref_id in managed_depts):
            return True

    # 团队组长：节点本身或父节点为该团队节点（团队属地授权）
    if can_manage and managed_teams:
        if (node.node_level == 3 and node.ref_id in managed_teams) or \
           (node.parent and node.parent.node_level == 3 and node.parent.ref_id in managed_teams):
            return True

    # 内容贡献者（显式授予 contributor 角色）：与上传权限一致，可管理本团队节点
    if perm['is_contributor'] and managed_teams:
        if (node.node_level == 3 and node.ref_id in managed_teams) or \
           (node.parent and node.parent.node_level == 3 and node.parent.ref_id in managed_teams):
            return True

    return False
