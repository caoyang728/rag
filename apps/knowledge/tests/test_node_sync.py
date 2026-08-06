"""
apps.knowledge.node_sync 测试 —— 部门/团队 ↔ KnowledgeNode 双向同步

覆盖范围：
- get_or_create_kb_root：根节点创建幂等 + path 修复
- sync_dept_node：创建 / 改名 / 排序号 / 软删除 / 恢复
- sync_team_node：无部门跳过 / 创建 / 改名 / 换部门 / 软删除 / 部门节点缺失
- get_subtree_node_ids：递归收集子树节点 ID
- count_docs_in_subtree：子树文档统计（不含已删除）

采用 DB 集成（django_db）：
node_sync 全部围绕 KnowledgeNode / Document 表做增改查，需真实 ORM
才能验证 path/depth/ref_id 落库结果与级联同步逻辑。
"""
import uuid

import pytest

from apps.knowledge.models import KnowledgeNode, Document
from apps.knowledge.node_sync import (
    get_or_create_kb_root,
    sync_dept_node,
    sync_team_node,
    get_subtree_node_ids,
    count_docs_in_subtree,
)
from apps.users.models import Department, Team, User


def _make_user(username='sync_user'):
    """创建测试用户"""
    return User.objects.create_user(
        username=username, password='testpass123', email=f'{username}@test.com')


def _make_doc(node, owner, title='同步文档'):
    """创建未删除文档（node 必需）"""
    return Document.objects.create(
        node=node,
        title=title,
        file_name=f'{title}.txt',
        file_type='txt',
        file_size=100,
        file_hash=uuid.uuid4().hex,
        file_path='/tmp/fake.txt',
        mime_type='text/plain',
        owner=owner,
        root_type=node.root_type,
        status='done',
    )


# ============================================================================
# get_or_create_kb_root
# ============================================================================
@pytest.mark.django_db
@pytest.mark.integration
class TestGetOrCreateKbRoot:
    """KB 根节点（level 1）获取/创建测试"""

    def test_create_then_idempotent(self):
        """首次调用创建根节点，再次调用应返回同一节点（幂等）"""
        root = get_or_create_kb_root()
        assert root.node_level == 1
        assert root.node_type == 'root'
        assert root.name == '知识库'
        assert root.parent is None
        assert root.path == f'/{root.id}/'
        assert root.depth == 0

        again = get_or_create_kb_root()
        assert again.id == root.id
        assert KnowledgeNode.objects.filter(node_level=1).count() == 1

    def test_repair_path_when_mismatch(self):
        """根节点 path 与 /{id}/ 不一致时应自动修复（迁移旧数据兜底）"""
        root = KnowledgeNode.objects.create(
            parent=None, root_type='knowledge_base', node_type='root',
            name='知识库', node_level=1, path='/stale/', depth=0)
        fixed = get_or_create_kb_root()
        assert fixed.id == root.id
        assert fixed.path == f'/{root.id}/'


# ============================================================================
# sync_dept_node
# ============================================================================
@pytest.mark.django_db
@pytest.mark.integration
class TestSyncDeptNode:
    """部门 → level 2 节点同步测试"""

    def _make_dept(self, name='研发部', sort_order=1, is_deleted=False):
        return Department.objects.create(
            name=name, sort_order=sort_order, is_deleted=is_deleted)

    def test_create_dept_node(self):
        """创建部门节点：挂在 KB root 下，ref_id=dept.id，path/depth 正确"""
        dept = self._make_dept(name='研发部', sort_order=3)
        node = sync_dept_node(dept)
        assert node is not None
        root = get_or_create_kb_root()
        assert node.parent_id == root.id
        assert node.node_level == 2
        assert node.node_type == 'folder'
        assert node.name == '研发部'
        assert node.order_no == 3
        assert node.ref_id == dept.id
        assert node.path == f'{root.path}{node.id}/'
        assert node.depth == 1

    def test_rename_dept_node(self):
        """部门改名后再次同步：节点名称应更新，ref_id 不变"""
        dept = self._make_dept(name='研发部')
        node = sync_dept_node(dept)
        dept.name = '技术部'
        node2 = sync_dept_node(dept)
        assert node2.id == node.id
        node2.refresh_from_db()
        assert node2.name == '技术部'
        assert node2.ref_id == dept.id

    def test_update_sort_order(self):
        """部门排序号变化应同步到节点 order_no"""
        dept = self._make_dept(name='研发部', sort_order=1)
        node = sync_dept_node(dept)
        dept.sort_order = 9
        node2 = sync_dept_node(dept)
        node2.refresh_from_db()
        assert node2.order_no == 9

    def test_soft_delete_dept_node(self):
        """部门软删除：对应 level 2 节点软删除，返回 None"""
        dept = self._make_dept(name='研发部')
        sync_dept_node(dept)
        dept.is_deleted = True
        result = sync_dept_node(dept)
        assert result is None
        node = KnowledgeNode.objects.get(node_level=2, ref_id=dept.id)
        assert node.is_deleted is True

    def test_restore_dept_node(self):
        """部门恢复：节点应解除软删除"""
        dept = self._make_dept(name='研发部')
        node = sync_dept_node(dept)
        dept.is_deleted = True
        sync_dept_node(dept)
        dept.is_deleted = False
        node2 = sync_dept_node(dept)
        node2.refresh_from_db()
        assert node2.id == node.id
        assert node2.is_deleted is False


# ============================================================================
# sync_team_node
# ============================================================================
@pytest.mark.django_db
@pytest.mark.integration
class TestSyncTeamNode:
    """团队 → level 3 节点同步测试"""

    def _make_dept(self, name='研发部'):
        return Department.objects.create(name=name, sort_order=1)

    def _make_team(self, dept, name='后端组', description=None, is_deleted=False):
        return Team.objects.create(
            name=name, department=dept,
            description=description, is_deleted=is_deleted)

    def test_team_without_department_skipped(self):
        """团队无归属部门：跳过同步，返回 None 且不建节点"""
        team = Team.objects.create(name='游离组', department=None)
        assert sync_team_node(team) is None
        assert not KnowledgeNode.objects.filter(node_level=3).exists()

    def test_create_team_node(self):
        """创建团队节点：挂在部门节点下，ref_id=team.id"""
        dept = self._make_dept()
        dept_node = sync_dept_node(dept)
        team = self._make_team(dept, name='后端组', description='负责后端')
        node = sync_team_node(team)
        assert node is not None
        assert node.parent_id == dept_node.id
        assert node.node_level == 3
        assert node.node_type == 'folder'
        assert node.name == '后端组'
        assert node.description == '负责后端'
        assert node.ref_id == team.id
        assert node.path == f'{dept_node.path}{node.id}/'
        assert node.depth == 2

    def test_rename_and_move_team(self):
        """团队改名 + 换部门：节点名称/描述/parent/path 全部更新"""
        dept1 = self._make_dept('研发部')
        dept2 = self._make_dept('产品部')
        dept1_node = sync_dept_node(dept1)
        dept2_node = sync_dept_node(dept2)
        team = self._make_team(dept1, name='后端组')
        node = sync_team_node(team)
        assert node.parent_id == dept1_node.id

        team.name = '中间件组'
        team.department = dept2
        team.description = '中间件研发'
        node2 = sync_team_node(team)
        node2.refresh_from_db()
        assert node2.id == node.id
        assert node2.name == '中间件组'
        assert node2.description == '中间件研发'
        assert node2.parent_id == dept2_node.id
        assert node2.path == f'{dept2_node.path}{node2.id}/'
        assert node2.depth == 2

    def test_soft_delete_team_node(self):
        """团队软删除：对应 level 3 节点软删除，返回 None"""
        dept = self._make_dept()
        sync_dept_node(dept)
        team = self._make_team(dept)
        sync_team_node(team)
        team.is_deleted = True
        assert sync_team_node(team) is None
        node = KnowledgeNode.objects.get(node_level=3, ref_id=team.id)
        assert node.is_deleted is True

    def test_missing_dept_node_warns(self):
        """团队归属部门的 level 2 节点不存在：返回 None，不创建团队节点"""
        dept = self._make_dept()
        team = self._make_team(dept)
        # 不先 sync_dept_node，让部门节点缺失
        assert sync_team_node(team) is None
        assert not KnowledgeNode.objects.filter(node_level=3).exists()


# ============================================================================
# get_subtree_node_ids / count_docs_in_subtree
# ============================================================================
@pytest.mark.django_db
@pytest.mark.integration
class TestSubtreeQueries:
    """子树节点收集与文档计数测试"""

    def _build_tree(self):
        """构造 3 层节点树：root -> dept -> team + 业务分类子节点"""
        root = get_or_create_kb_root()
        dept = Department.objects.create(name='研发部', sort_order=1)
        dept_node = sync_dept_node(dept)
        team = Team.objects.create(name='后端组', department=dept)
        team_node = sync_team_node(team)
        leaf = KnowledgeNode.objects.create(
            parent=team_node, root_type=root.root_type, node_type='leaf',
            node_level=4, name='子分类')
        leaf.path = f'{team_node.path}{leaf.id}/'
        leaf.depth = 3
        leaf.save(update_fields=['path', 'depth'])
        return root, dept_node, team_node, leaf

    def test_get_subtree_node_ids_includes_descendants(self):
        """收集结果应包含自身 + 全部子孙节点 ID"""
        _, dept_node, team_node, leaf = self._build_tree()
        ids = get_subtree_node_ids(dept_node.id)
        assert set(ids) == {dept_node.id, team_node.id, leaf.id}

    def test_get_subtree_node_ids_leaf_only(self):
        """叶节点无子孙：仅返回自身"""
        _, _, _, leaf = self._build_tree()
        assert get_subtree_node_ids(leaf.id) == [leaf.id]

    def test_get_subtree_node_ids_skips_deleted(self):
        """软删除的子孙节点不应被收集"""
        _, dept_node, team_node, leaf = self._build_tree()
        KnowledgeNode.objects.filter(id=team_node.id).update(is_deleted=True)
        ids = get_subtree_node_ids(dept_node.id)
        assert set(ids) == {dept_node.id, leaf.id}

    def test_count_docs_in_subtree(self):
        """统计子树全部未删除文档，且排除已删除文档与子树外文档"""
        _, dept_node, team_node, leaf = self._build_tree()
        user = _make_user()
        _make_doc(team_node, user, '团队节点文档')
        _make_doc(leaf, user, '子节点文档')
        deleted_doc = _make_doc(leaf, user, '已删除文档')
        Document.objects.filter(id=deleted_doc.id).update(is_deleted=True)
        # 子树外文档（挂在另一个节点下）
        other = KnowledgeNode.objects.create(
            parent=None, root_type='knowledge_base', node_type='folder',
            node_level=2, name='其他节点', ref_id=999)
        other.path = f'/{other.id}/'
        other.depth = 1
        other.save(update_fields=['path', 'depth'])
        _make_doc(other, user, '子树外文档')

        assert count_docs_in_subtree(dept_node.id) == 2
