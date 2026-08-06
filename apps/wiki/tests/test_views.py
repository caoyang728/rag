"""
apps.wiki.views 接口集成测试 —— Wiki 页面浏览 / 生成 / 刷新 / 过期 API

覆盖范围：
- 认证拦截：匿名访问应 401
- WikiPageViewSet.list：按节点权限过滤（仅返回用户可读节点的 Wiki）
- WikiPageViewSet.retrieve：可读返回 200（含正文/章节/链接），不可读返回 403
- WikiPageGenerateView：有管理权限提交异步任务；无权限 403
- refresh / expire：管理权限判定
- community 页面：仅系统管理员 / 知识库管理员可见

采用 pytest-django（django_db）+ JWT：
权限链路（resolve_doc_access / can_manage_wiki）需要真实 DB 与 RBAC 数据，
mock Celery 任务避免触发真实 LLM 生成。
"""
import json
import uuid as uuid_lib
from unittest.mock import patch

import pytest
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken

from apps.graph.models import GraphCommunity
from apps.knowledge.models import (
    KnowledgeNode, Document, VisibilityLevel,
)
from apps.users.models import (
    User, Role, UserRoleRel, Permission, RolePermissionRel,
    Department, Team, GrantStatus,
)
from apps.wiki.models import WikiPage, WikiSection, WikiLink


# ============================================================================
# 测试辅助函数（复用 knowledge 测试的基建模式）
# ============================================================================
def _get_or_create_role(role_key, **defaults):
    """获取或创建内置角色，补齐默认字段"""
    default_map = {
        'super_admin': dict(name='超级管理员', is_builtin=True),
        'viewer': dict(name='查看者', is_builtin=True),
        'team_leader': dict(name='团队组长', is_builtin=True),
    }
    defaults = {**default_map.get(role_key, {}), **defaults}
    role, _ = Role.objects.get_or_create(role_key=role_key, defaults=defaults)
    return role


def _create_test_user(username, password='testpass123', is_super_admin=False):
    """创建测试用户，is_super_admin 时授予 super_admin 角色（user.is_super_admin 是属性）"""
    user = User.objects.create_user(
        username=username, password=password, email=f'{username}@test.com')
    if is_super_admin:
        admin_role = _get_or_create_role('super_admin')
        UserRoleRel.objects.get_or_create(
            user=user, role=admin_role,
            defaults={'status': GrantStatus.ACTIVE})
    return user


def _grant_user_manage(user):
    """授予 user.manage 权限（模拟团队组长/部门经理写权限），并绑定团队属地"""
    perm, _ = Permission.objects.get_or_create(
        permission_key='user.manage',
        defaults={'permission_name': '用户管理', 'module': 'user', 'is_builtin': True})
    role = _get_or_create_role('team_leader')
    RolePermissionRel.objects.get_or_create(
        role=role, permission=perm, defaults={'is_active': True})
    UserRoleRel.objects.get_or_create(
        user=user, role=role, defaults={'status': GrantStatus.ACTIVE})


def _auth_headers(user):
    """构造 JWT 认证 header"""
    return {'HTTP_AUTHORIZATION': f'Bearer {RefreshToken.for_user(user).access_token}'}


def _create_document(node, owner, visibility_level=VisibilityLevel.TEAM_ONLY,
                     team_id=None, dept_id=None, title='测试文档', **extra):
    """创建已完成解析的文档（绕过上传管线，status='done'）"""
    return Document.objects.create(
        node=node,
        title=title,
        file_name=f'{title}-{uuid_lib.uuid4().hex[:8]}.txt',
        file_type='txt',
        file_size=100,
        file_hash=uuid_lib.uuid4().hex,
        file_path='/tmp/fake.txt',
        mime_type='text/plain',
        owner=owner,
        dept_id=dept_id,
        team_id=team_id,
        visibility_level=visibility_level,
        root_type=node.root_type,
        status='done',
        **extra,
    )


def _create_wiki_page(node, title='节点 Wiki', status='published', **extra):
    """创建挂载在节点上的 Wiki 页面"""
    return WikiPage.objects.create(
        node=node, title=title,
        summary='这是摘要',
        content=f'# {title}\n\n正文内容',
        status=status,
        tags=['company_doc'],
        **extra,
    )


# ============================================================================
# 测试基类
# ============================================================================
@pytest.mark.django_db
class WikiViewsTestBase:
    """Wiki 接口测试公共基类

    组织结构：研发部 → 后端组 → 业务分类节点
    文档矩阵：
    - doc_own_private：normal_user 自己的 TEAM_ONLY 文档（normal_user 可读）
    - doc_other_public：超管的 PUBLIC 文档（全员可读）
    - doc_hidden：超管的 TEAM_ONLY 文档（normal_user 不可读，挂在 other_node）
    Wiki 页面：
    - wiki_visible：挂在 category_node（normal_user 可读）
    - wiki_hidden：挂在 other_node（normal_user 不可读）
    - wiki_community：挂在图谱社区（仅管理员可见）
    """

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入 client/组织架构/节点树/文档矩阵/Wiki 页面（DB 每测试隔离）"""
        self.client = Client()
        _get_or_create_role('viewer')

        self.super_admin = _create_test_user('admin', is_super_admin=True)
        self.normal_user = _create_test_user('normal')
        self.team_leader = _create_test_user('leader')

        # 组织架构
        self.dept = Department.objects.create(name='研发部', code='rd')
        self.team = Team.objects.create(
            name='后端组', code='rd-backend', department=self.dept,
            leader=self.team_leader)
        # 团队组长绑定本团队（get_user_managed_teams 依赖 user.team_id / 属地授权）
        self.team_leader.team = self.team
        self.team_leader.save(update_fields=['team'])

        # 知识节点树：root → dept → team → 业务分类 + 另一个不可读节点
        self.root_node = self._create_node('知识库', 'root', node_level=1)
        self.dept_node = self._create_node(
            '研发部', 'folder', node_level=2, parent=self.root_node,
            ref_id=self.dept.id)
        self.team_node = self._create_node(
            '后端组', 'folder', node_level=3, parent=self.dept_node,
            ref_id=self.team.id)
        self.category_node = self._create_node(
            '业务分类', 'folder', node_level=4, parent=self.team_node)
        self.other_node = self._create_node(
            '机密分类', 'folder', node_level=4, parent=self.team_node)

        # 文档矩阵
        self.doc_own_private = _create_document(
            self.category_node, self.normal_user,
            visibility_level=VisibilityLevel.TEAM_ONLY,
            team_id=self.team.id, dept_id=self.dept.id, title='我的私有文档')
        self.doc_other_public = _create_document(
            self.category_node, self.super_admin,
            visibility_level=VisibilityLevel.PUBLIC,
            team_id=self.team.id, dept_id=self.dept.id, title='他人公开文档')
        self.doc_hidden = _create_document(
            self.other_node, self.super_admin,
            visibility_level=VisibilityLevel.TEAM_ONLY,
            team_id=self.team.id, dept_id=self.dept.id, title='他人私有文档')

        # Wiki 页面矩阵
        self.wiki_visible = _create_wiki_page(self.category_node, '业务分类 Wiki')
        self.wiki_visible_expired = _create_wiki_page(
            self.category_node, '已过期 Wiki', status='expired')
        self.wiki_hidden = _create_wiki_page(self.other_node, '机密 Wiki')

        # 图谱社区 Wiki（community_id + level 联合主键）
        self.community = GraphCommunity.objects.create(
            community_id=1, level=0, summary='社区摘要',
            keywords=['图谱'], metadata={'topic': '图谱领域'})
        self.wiki_community = WikiPage.objects.create(
            community=self.community, title='社区 Wiki',
            summary='社区摘要', content='社区正文', status='published')

    def _create_node(self, name, node_type, node_level, parent=None, ref_id=None):
        """创建节点并回填 path（路径枚举 /id1/id2/.../，4 位零填充）"""
        node = KnowledgeNode.objects.create(
            name=name, node_type=node_type, node_level=node_level,
            root_type='company_doc', parent=parent, ref_id=ref_id,
            depth=(parent.depth + 1) if parent else 0,
            created_by=self.super_admin,
        )
        padded = f'{node.id:04d}'
        node.path = f'{parent.path}{padded}/' if parent else f'/{padded}/'
        node.save(update_fields=['path'])
        return node

    def _results(self, resp):
        """兼容分页/非分页响应，提取结果列表"""
        data = resp.json()
        return data['results'] if isinstance(data, dict) and 'results' in data else data


# ============================================================================
# 认证拦截
# ============================================================================
class TestWikiAuthRequired(WikiViewsTestBase):
    """匿名访问应被认证拦截"""

    @pytest.mark.integration
    def test_list_anonymous_401(self):
        """匿名访问 Wiki 列表应 401/403"""
        resp = self.client.get('/api/v1/wiki/pages/')
        assert resp.status_code in (401, 403)

    @pytest.mark.integration
    def test_generate_anonymous_401(self):
        """匿名触发生成应 401/403"""
        resp = self.client.post('/api/v1/wiki/pages/generate/',
                                data=json.dumps({'node_id': 1}),
                                content_type='application/json')
        assert resp.status_code in (401, 403)


# ============================================================================
# Wiki 列表（权限过滤 + 搜索 + 过滤）
# ============================================================================
class TestWikiList(WikiViewsTestBase):
    """Wiki 列表权限过滤与检索测试"""

    @pytest.mark.integration
    def test_list_returns_only_accessible_pages(self):
        """普通用户仅看到可读节点的 Wiki（含过期页面），看不到机密节点与社区 Wiki"""
        resp = self.client.get('/api/v1/wiki/pages/', **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        ids = [r['id'] for r in data['results']]
        assert self.wiki_visible.id in ids
        assert self.wiki_visible_expired.id in ids
        assert self.wiki_hidden.id not in ids
        assert self.wiki_community.id not in ids
        assert data['count'] == 2

    @pytest.mark.integration
    def test_list_super_admin_sees_all(self):
        """管理员看到全部 Wiki 页面（含社区页面）"""
        resp = self.client.get('/api/v1/wiki/pages/', **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        ids = [r['id'] for r in resp.json()['results']]
        assert self.wiki_community.id in ids
        assert self.wiki_hidden.id in ids

    @pytest.mark.integration
    def test_list_search_by_title(self):
        """q 参数按标题/摘要/标签搜索"""
        resp = self.client.get(
            '/api/v1/wiki/pages/?q=过期',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        ids = [r['id'] for r in resp.json()['results']]
        assert self.wiki_visible_expired.id in ids
        assert self.wiki_visible.id not in ids

    @pytest.mark.integration
    def test_list_filter_by_status_and_node(self):
        """status / node_id 过滤"""
        resp = self.client.get(
            f'/api/v1/wiki/pages/?status=published&node_id={self.category_node.id}',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        ids = [r['id'] for r in resp.json()['results']]
        assert self.wiki_visible.id in ids
        assert self.wiki_visible_expired.id not in ids

    @pytest.mark.integration
    def test_list_includes_can_manage_flag(self):
        """列表返回 can_manage：普通用户无管理权限为 False，管理员为 True"""
        resp = self.client.get(
            f'/api/v1/wiki/pages/?node_id={self.category_node.id}',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        rows = {r['id']: r for r in resp.json()['results']}
        row = rows[self.wiki_visible.id]
        assert row['can_manage'] is False
        assert row['node_name'] == '业务分类'
        assert row['status_label'] == '已发布'
        assert rows[self.wiki_visible_expired.id]['status_label'] == '已过期'


# ============================================================================
# Wiki 详情
# ============================================================================
class TestWikiDetail(WikiViewsTestBase):
    """Wiki 详情接口测试"""

    @pytest.mark.integration
    def test_retrieve_accessible_returns_content(self):
        """可读页面返回正文 / 章节 / 链接，并递增阅读计数"""
        section = WikiSection.objects.create(
            page=self.wiki_visible, title='第一章', content='章节内容', order=0)
        resp = self.client.get(
            f'/api/v1/wiki/pages/{self.wiki_visible.id}/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data['content'].startswith('# 业务分类 Wiki')
        assert data['sections'][0]['title'] == '第一章'
        assert data['sections'][0]['content'] == '章节内容'
        assert 'outgoing_links' in data and 'incoming_links' in data
        # 阅读计数 +1
        self.wiki_visible.refresh_from_db()
        assert self.wiki_visible.view_count == 1

    @pytest.mark.integration
    def test_retrieve_inaccessible_403(self):
        """不可读页面（机密节点）返回 403"""
        resp = self.client.get(
            f'/api/v1/wiki/pages/{self.wiki_hidden.id}/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_retrieve_community_non_admin_403(self):
        """普通用户访问社区 Wiki 详情返回 403"""
        resp = self.client.get(
            f'/api/v1/wiki/pages/{self.wiki_community.id}/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_retrieve_community_admin_200(self):
        """管理员可访问社区 Wiki 详情"""
        resp = self.client.get(
            f'/api/v1/wiki/pages/{self.wiki_community.id}/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        assert resp.json()['title'] == '社区 Wiki'

    @pytest.mark.integration
    def test_retrieve_not_found_404(self):
        """不存在的页面返回 404"""
        resp = self.client.get(
            '/api/v1/wiki/pages/999999/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 404


# ============================================================================
# 手动生成
# ============================================================================
class TestWikiGenerate(WikiViewsTestBase):
    """手动触发生成测试"""

    @pytest.mark.integration
    @patch('apps.wiki.tasks.generate_wiki_for_node')
    def test_generate_super_admin_ok(self, mock_task):
        """管理员为节点触发生成 → 提交异步任务"""
        resp = self.client.post(
            '/api/v1/wiki/pages/generate/',
            data=json.dumps({'node_id': self.category_node.id}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        assert resp.json()['ok'] is True
        mock_task.delay.assert_called_once_with(self.category_node.id)

    @pytest.mark.integration
    @patch('apps.wiki.tasks.generate_wiki_for_node')
    def test_generate_team_leader_ok(self, mock_task):
        """团队组长（有 user.manage + 本团队节点）可触发生成"""
        _grant_user_manage(self.team_leader)
        resp = self.client.post(
            '/api/v1/wiki/pages/generate/',
            data=json.dumps({'node_id': self.team_node.id}),
            content_type='application/json',
            **_auth_headers(self.team_leader))
        assert resp.status_code == 200
        mock_task.delay.assert_called_once_with(self.team_node.id)

    @pytest.mark.integration
    def test_generate_normal_user_403(self):
        """普通用户无管理权限 → 403"""
        resp = self.client.post(
            '/api/v1/wiki/pages/generate/',
            data=json.dumps({'node_id': self.category_node.id}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_generate_missing_node_id_400(self):
        """缺少 node_id → 400"""
        resp = self.client.post(
            '/api/v1/wiki/pages/generate/',
            data=json.dumps({}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_generate_node_not_found_404(self):
        """节点不存在 → 404"""
        resp = self.client.post(
            '/api/v1/wiki/pages/generate/',
            data=json.dumps({'node_id': 999999}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 404


# ============================================================================
# 刷新 / 过期
# ============================================================================
class TestWikiRefreshAndExpire(WikiViewsTestBase):
    """刷新与过期管理动作测试"""

    @pytest.mark.integration
    @patch('apps.wiki.tasks.generate_wiki_for_node')
    def test_refresh_super_admin_ok(self, mock_task):
        """管理员刷新节点 Wiki → 提交重新生成任务"""
        resp = self.client.post(
            f'/api/v1/wiki/pages/{self.wiki_visible.id}/refresh/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        mock_task.delay.assert_called_once_with(self.category_node.id)

    @pytest.mark.integration
    def test_refresh_normal_user_403(self):
        """普通用户刷新 → 403"""
        resp = self.client.post(
            f'/api/v1/wiki/pages/{self.wiki_visible.id}/refresh/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_refresh_community_page_400(self):
        """社区 Wiki 不支持刷新 → 400"""
        resp = self.client.post(
            f'/api/v1/wiki/pages/{self.wiki_community.id}/refresh/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_expire_super_admin_ok(self):
        """管理员标记过期 → status 变为 expired"""
        resp = self.client.post(
            f'/api/v1/wiki/pages/{self.wiki_visible.id}/expire/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        self.wiki_visible.refresh_from_db()
        assert self.wiki_visible.status == 'expired'

    @pytest.mark.integration
    def test_expire_normal_user_403(self):
        """普通用户标记过期 → 403"""
        resp = self.client.post(
            f'/api/v1/wiki/pages/{self.wiki_visible.id}/expire/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403
