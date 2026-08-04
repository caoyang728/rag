"""
部门/团队 ↔ KnowledgeNode 双向同步

触发时机：Department / Team 的 post_save 信号
- 创建 → 在 KB root 下创建对应层级的知识节点
- 更新（改名） → 通过 ref_id 定位节点，同步名称
- 软删除 → 同步软删除节点
- 恢复 → 同步恢复节点

节点树层级：
  Level 1: KB root（知识库根节点，自动创建）
  Level 2: 部门节点（folder，ref_id = dept.id）
  Level 3: 团队节点（folder，ref_id = team.id，可包含业务分类子节点）
  Level 4+: 业务分类节点（由组长手动管理，无 ref_id）
"""
from loguru import logger

from apps.knowledge.models import KnowledgeNode, Document


# TODO: 当前系统仅支持单一领域(knowledge_base),需扩展为多领域架构:
# 1. 上传文档时需选择领域(如 公司文档/技术资料/产品文档/HR人事/财务/法务/运营SOP 等),
#    现有上传逻辑仅要求 node_id,领域由节点树隐式继承,无法让用户显式选择
# 2. 需支持创建多个 KB 根节点(每个根节点一个领域),而非当前的 get_or_create 单根
# 3. 领域列表建议按业务功能划分(非组织架构维度),组织架构已由 部门/团队节点层级表达
_ROOT_TYPE = 'knowledge_base'
_KB_ROOT_NAME = '知识库'


# ── KB Root ──────────────────────────────────────────────────
def get_or_create_kb_root() -> KnowledgeNode:
    """获取或创建知识库根节点（level 1）"""
    node, created = KnowledgeNode.objects.get_or_create(
        parent=None,
        node_level=1,
        defaults={
            'root_type': _ROOT_TYPE,
            'node_type': 'root',
            'name': _KB_ROOT_NAME,
            'path': '/',
            'depth': 0,
        }
    )
    expected_path = f'/{node.id}/'
    if node.path != expected_path:
        node.path = expected_path
        node.save(update_fields=['path'])
    if created:
        logger.info(f'[NodeSync] 创建 KB 根节点: id={node.id}')
    return node


# ── Department (level 2) ──────────────────────────────────────
def sync_dept_node(department) -> KnowledgeNode | None:
    """部门 → KnowledgeNode（level 2），通过 ref_id=department.id 稳定追踪"""

    # 软删除
    if department.is_deleted:
        KnowledgeNode.objects.filter(
            node_level=2,
            ref_id=department.id,
        ).update(is_deleted=True)
        logger.info(f'[NodeSync] 软删除部门节点: {department.name} (dept_id={department.id})')
        return None

    kb_root = get_or_create_kb_root()

    # 通过 ref_id 查找已有节点（支持改名）
    node = KnowledgeNode.objects.filter(
        parent=kb_root,
        node_level=2,
        ref_id=department.id,
    ).first()

    if node:
        updated = False
        if node.is_deleted:
            node.is_deleted = False
            updated = True
        if node.name != department.name:
            node.name = department.name
            updated = True
        if node.order_no != department.sort_order:
            node.order_no = department.sort_order
            updated = True
        if updated:
            node.save()
        return node

    # 创建新节点
    node = KnowledgeNode.objects.create(
        parent=kb_root,
        root_type=kb_root.root_type,
        node_type='folder',
        node_level=2,
        name=department.name,
        order_no=department.sort_order,
        ref_id=department.id,
    )
    node.path = f'{kb_root.path}{node.id}/'
    node.depth = kb_root.depth + 1
    node.save(update_fields=['path', 'depth'])
    logger.info(f'[NodeSync] 创建部门节点: {department.name} (node={node.id}, dept_id={department.id})')
    return node


# ── Team (level 3) ───────────────────────────────────────────
def sync_team_node(team) -> KnowledgeNode | None:
    """团队 → KnowledgeNode（level 3），通过 ref_id=team.id 稳定追踪"""
    if not team.department:
        logger.debug(f'[NodeSync] 团队 {team.name} 无归属部门，跳过节点同步')
        return None

    # 软删除
    if team.is_deleted:
        KnowledgeNode.objects.filter(
            node_level=3,
            ref_id=team.id,
        ).update(is_deleted=True)
        logger.info(f'[NodeSync] 软删除团队节点: {team.name} (team_id={team.id})')
        return None

    # 查找部门节点
    dept_node = KnowledgeNode.objects.filter(
        parent__node_level=1,
        node_level=2,
        ref_id=team.department_id,
        is_deleted=False,
    ).first()

    if not dept_node:
        logger.warning(f'[NodeSync] 团队 {team.name} 的部门节点(dept_id={team.department_id}) 不存在')
        return None

    # 通过 ref_id 查找已有节点（支持改名/换部门）
    node = KnowledgeNode.objects.filter(
        node_level=3,
        ref_id=team.id,
    ).first()

    # 如果团队的父部门变了，更新 parent
    parent_changed = node and node.parent_id != dept_node.id

    if node:
        updated = False
        if node.is_deleted:
            node.is_deleted = False
            updated = True
        if node.name != team.name:
            node.name = team.name
            updated = True
        desc = team.description or ''
        if node.description != desc:
            node.description = desc
            updated = True
        if parent_changed:
            node.parent = dept_node
            node.path = f'{dept_node.path}{node.id}/'
            node.depth = dept_node.depth + 1
            updated = True
        if updated:
            node.save()
        return node

    # 创建新节点
    node = KnowledgeNode.objects.create(
        parent=dept_node,
        root_type=dept_node.root_type,
        node_type='folder',
        node_level=3,
        name=team.name,
        description=team.description or '',
        ref_id=team.id,
    )
    node.path = f'{dept_node.path}{node.id}/'
    node.depth = dept_node.depth + 1
    node.save(update_fields=['path', 'depth'])
    logger.info(f'[NodeSync] 创建团队节点: {team.name} (node={node.id}, team_id={team.id})')
    return node


# ── 删除辅助 ────────────────────────────────────────────────
def get_subtree_node_ids(node_id: int) -> list[int]:
    """递归收集 node_id 及其所有子孙节点的 ID 列表（用于文档计数等）"""
    ids = [node_id]
    children = KnowledgeNode.objects.filter(
        parent_id=node_id, is_deleted=False
    ).values_list('id', flat=True)
    for child_id in children:
        ids.extend(get_subtree_node_ids(child_id))
    return ids


def count_docs_in_subtree(node_id: int) -> int:
    """统计某个节点及其所有子孙节点下未删除的文档数量"""
    subtree_ids = get_subtree_node_ids(node_id)
    if not subtree_ids:
        return 0
    return Document.objects.filter(
        node_id__in=subtree_ids, is_deleted=False
    ).count()
