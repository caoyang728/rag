"""
apps.wiki.access 权限判定单元测试 —— Wiki 页面访问与管理权限

覆盖范围：
- get_accessible_node_ids：匿名/无已完成文档/可读文档 → 节点集合
- can_read_wiki：匿名拒绝、管理员放行、节点页（可读/不可读）、社区页（非管理员拒绝）
- can_manage_wiki：匿名拒绝、管理员放行、部门经理（自身/父节点属地）、
  团队组长（自身/父节点属地）、内容贡献者（本团队节点）、无权限用户拒绝

采用 pytest-django（django_db）：
权限链路（RBAC 角色/权限点、部门/团队属地、文档访问判定）需要真实 DB 数据，
组织架构与角色基建复用 test_views.py 的既有模式。
"""
import pytest
from django.contrib.auth.models import AnonymousUser

from apps.graph.models import GraphCommunity
from apps.knowledge.models import KnowledgeNode, Document, VisibilityLevel
from apps.users.models import (
    User, Role, UserRoleRel, Permission, RolePermissionRel,
    Department, Team, GrantStatus,
)
from apps.wiki.models import WikiPage
from apps.wiki.access import (
    get_accessible_node_ids, can_read_wiki, can_manage_wiki,
)


# ============================================================================
# 测试辅助函数（复用 test_views 的基建模式）
# ============================================================================
def _get_or_create_role(role_key, **defaults):
    """获取或创建内置角色，补齐默认字段"""
    default_map = {
        'super_admin': dict(name='超级管理员', is_builtin=True),
        'dept_manager': dict(name='部门经理', is_builtin=True),
        'team_leader': dict(name='团队组长', is_builtin=True),
        'contributor': dict(name='内容贡献者', is_builtin=True),
    }
    defaults = {**default_map.get(role_key, {}), **defaults}
    role, _ = Role.objects.get_or_create(role_key=role_key, defaults=defaults)
    return role


def _create_test_user(username, password='testpass123', is_super_admin=False):
    """创建测试用户，is_super_admin 时授予 super_admin 角色"""
    user = User.objects.create_user(
        username=username, password=password, email=f'{username}@test.com')
    if is_super_admin:
        admin_role = _get_or_create_role('super_admin')
        UserRoleRel.objects.get_or_create(
            user=user, role=admin_role,
            defaults={'status': GrantStatus.ACTIVE})
    return user


def _grant_user_manage(user, role_key='team_leader'):
    """授予 user.manage 权限（团队组长/部门经理写权限）"""
    perm, _ = Permission.objects.get_or_create(
        permission_key='user.manage',
        defaults={'permission_name': '用户管理', 'module': 'user', 'is_builtin': True})
    role = _get_or_create_role(role_key)
    RolePermissionRel.objects.get_or_create(
        role=role, permission=perm, defaults={'is_active': True})
    UserRoleRel.objects.get_or_create(
        user=user, role=role, defaults={'status': GrantStatus.ACTIVE})


def _grant_contributor(user):
    """显式授予内容贡献者角色"""
    role = _get_or_create_role('contributor')
    UserRoleRel.objects.get_or_create(
        user=user, role=role, defaults={'status': GrantStatus.ACTIVE})


# ============================================================================
# 测试基类：组织架构 + 节点树
# ============================================================================
@pytest.mark.django_db
class AccessTestBase:
    """组织架构：研发部 → 后端组 → 业务分类；节点树：root → dept → team → category"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self.admin = _create_test_user('acc-admin', is_super_admin=True)
        self.normal_user = _create_test_user('acc-normal')
        self.manager = _create_test_user('acc-manager')
        self.leader = _create_test_user('acc-leader')
        self.contributor = _create_test_user('acc-contributor')

        self.dept = Department.objects.create(name='研发部', code='acc-rd')
        self.team = Team.objects.create(
            name='后端组', code='acc-rd-backend', department=self.dept,
            leader=self.leader)
        self.leader.team = self.team
        self.leader.save(update_fields=['team'])

        self.root_node = self._create_node('知识库', 'root', node_level=1)
        self.dept_node = self._create_node(
            '研发部', 'folder', node_level=2, parent=self.root_node,
            ref_id=self.dept.id)
        self.team_node = self._create_node(
            '后端组', 'folder', node_level=3, parent=self.dept_node,
            ref_id=self.team.id)
        self.category_node = self._create_node(
            '业务分类', 'folder', node_level=4, parent=self.team_node)

    def _create_node(self, name, node_type, node_level, parent=None, ref_id=None):
        """创建节点并回填 path（路径枚举 /id1/id2/.../，4 位零填充）"""
        node = KnowledgeNode.objects.create(
            name=name, node_type=node_type, node_level=node_level,
            root_type='company_doc', parent=parent, ref_id=ref_id,
            depth=(parent.depth + 1) if parent else 0,
            created_by=self.admin,
        )
        padded = f'{node.id:04d}'
        node.path = f'{parent.path}{padded}/' if parent else f'/{padded}/'
        node.save(update_fields=['path'])
        return node

    def _create_doc(self, node, owner, status='done', **extra):
        """创建文档（绕过上传管线，默认 PUBLIC 已完成）"""
        return Document.objects.create(
            node=node, title=f'doc-{node.id}',
            file_name=f'doc-{node.id}.txt', file_type='txt',
            file_size=100, file_hash=f'h{node.id}-{owner.id}',
            file_path='/tmp/fake.txt', mime_type='text/plain',
            owner=owner, dept_id=self.dept.id, team_id=self.team.id,
            visibility_level=VisibilityLevel.PUBLIC,
            root_type=node.root_type, status=status, **extra,
        )

    def _make_page(self, node=None, community=None):
        return WikiPage.objects.create(
            node=node, community=community, title='权限测试页',
            summary='摘要', content='正文')


# ============================================================================
# get_accessible_node_ids —— 可读节点集合
# ============================================================================
class TestGetAccessibleNodeIds(AccessTestBase):
    """get_accessible_node_ids 节点集合计算"""

    @pytest.mark.integration
    def test_none_user_returns_empty(self):
        """user 为 None 时返回空集"""
        assert get_accessible_node_ids(None) == set()

    @pytest.mark.integration
    def test_anonymous_returns_empty(self):
        """匿名用户返回空集"""
        assert get_accessible_node_ids(AnonymousUser()) == set()

    @pytest.mark.integration
    def test_no_done_docs_returns_empty(self):
        """节点下无已完成文档时返回空集（不走权限过滤）"""
        self._create_doc(self.category_node, self.admin, status='pending')
        assert get_accessible_node_ids(self.normal_user, [self.category_node.id]) == set()

    @pytest.mark.integration
    def test_accessible_node_returned(self):
        """节点下有可读已完成文档时返回该节点 ID"""
        self._create_doc(self.category_node, self.admin, status='done')
        result = get_accessible_node_ids(self.normal_user, [self.category_node.id])
        assert result == {self.category_node.id}


# ============================================================================
# can_read_wiki —— 浏览权限
# ============================================================================
class TestCanReadWiki(AccessTestBase):
    """can_read_wiki 浏览权限判定"""

    @pytest.mark.integration
    def test_none_user_false(self):
        """user 为 None 时拒绝浏览"""
        page = self._make_page(node=self.category_node)
        assert can_read_wiki(None, page) is False

    @pytest.mark.integration
    def test_anonymous_false(self):
        """匿名用户拒绝浏览"""
        page = self._make_page(node=self.category_node)
        assert can_read_wiki(AnonymousUser(), page) is False

    @pytest.mark.integration
    def test_super_admin_true(self):
        """管理员拥有绝对浏览权限"""
        page = self._make_page(node=self.category_node)
        assert can_read_wiki(self.admin, page) is True

    @pytest.mark.integration
    def test_node_page_accessible(self):
        """节点页：用户可读该节点任一已完成文档即可浏览"""
        self._create_doc(self.category_node, self.admin, status='done')
        page = self._make_page(node=self.category_node)
        assert can_read_wiki(self.normal_user, page) is True

    @pytest.mark.integration
    def test_node_page_inaccessible(self):
        """节点页：无可读文档时拒绝浏览"""
        page = self._make_page(node=self.category_node)
        assert can_read_wiki(self.normal_user, page) is False

    @pytest.mark.integration
    def test_community_page_non_admin_false(self):
        """社区页：无文档权限维度，非管理员不可读"""
        community = GraphCommunity.objects.create(
            community_id=1, level=0, summary='社区摘要',
            keywords=['图谱'], metadata={'topic': '图谱领域'})
        page = self._make_page(community=community)
        assert can_read_wiki(self.normal_user, page) is False

    @pytest.mark.integration
    def test_community_page_admin_true(self):
        """社区页：管理员可读"""
        community = GraphCommunity.objects.create(
            community_id=2, level=0, summary='社区摘要',
            keywords=['图谱'], metadata={'topic': '图谱领域'})
        page = self._make_page(community=community)
        assert can_read_wiki(self.admin, page) is True


# ============================================================================
# can_manage_wiki —— 管理权限（触发生成/刷新/过期）
# ============================================================================
class TestCanManageWiki(AccessTestBase):
    """can_manage_wiki 管理权限判定"""

    @pytest.mark.integration
    def test_none_user_false(self):
        """user 为 None 时无管理权限"""
        assert can_manage_wiki(None, self.category_node) is False

    @pytest.mark.integration
    def test_anonymous_false(self):
        """匿名用户无管理权限"""
        assert can_manage_wiki(AnonymousUser(), self.category_node) is False

    @pytest.mark.integration
    def test_admin_true(self):
        """系统管理员可管理全部节点"""
        assert can_manage_wiki(self.admin, self.category_node) is True

    @pytest.mark.integration
    def test_dept_manager_manages_dept_and_child(self):
        """部门经理：user.manage + 本部门节点或其子节点可管理"""
        _grant_user_manage(self.manager, role_key='dept_manager')
        self.manager.department_id = self.dept.id
        self.manager.save(update_fields=['department_id'])

        # 节点本身为部门节点（node_level=2，ref_id 命中属地）
        assert can_manage_wiki(self.manager, self.dept_node) is True
        # 父节点为部门节点（团队节点挂在部门节点下）
        assert can_manage_wiki(self.manager, self.team_node) is True
        # 与部门无属地关系的节点（业务分类，父节点为团队节点）
        assert can_manage_wiki(self.manager, self.category_node) is False

    @pytest.mark.integration
    def test_team_leader_manages_own_team_node(self):
        """团队组长：user.manage + 本团队节点或其子节点可管理"""
        _grant_user_manage(self.leader, role_key='team_leader')

        # 节点本身为团队节点（node_level=3，ref_id 命中本团队）
        assert can_manage_wiki(self.leader, self.team_node) is True
        # 父节点为团队节点（业务分类挂在团队节点下）
        assert can_manage_wiki(self.leader, self.category_node) is True
        # 与团队无属地关系的节点
        assert can_manage_wiki(self.leader, self.dept_node) is False

    @pytest.mark.integration
    def test_contributor_manages_own_team_node(self):
        """内容贡献者：本团队节点或其子节点可管理"""
        _grant_contributor(self.contributor)
        self.contributor.team = self.team
        self.contributor.save(update_fields=['team'])

        assert can_manage_wiki(self.contributor, self.team_node) is True
        assert can_manage_wiki(self.contributor, self.category_node) is True

    @pytest.mark.integration
    def test_contributor_without_team_cannot_manage(self):
        """内容贡献者：无团队归属时不可管理"""
        _grant_contributor(self.contributor)
        assert can_manage_wiki(self.contributor, self.team_node) is False

    @pytest.mark.integration
    def test_normal_user_cannot_manage(self):
        """普通用户无任何管理角色 → False"""
        assert can_manage_wiki(self.normal_user, self.category_node) is False
