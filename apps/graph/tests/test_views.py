"""
apps.graph.views 接口集成测试 —— 图谱可视化与实体检索 API

覆盖范围：
- 认证拦截：匿名访问应 401
- 实体列表 / 详情：按来源文档权限过滤（无权限实体不出现在结果中）
- 语义检索：向量命中结果做权限过滤
- 邻居子图：1~2 跳扩展 + 关系仅两端可见时返回
- 社区列表 / 详情：含任一可见实体即可见
- 手动触发社区检测：仅知识库管理员 / 超管可执行

采用 pytest-django（django_db）+ JWT；语义检索 mock 向量生成与向量召回，
社区检测 mock Celery 任务，避免触发真实 LLM / 向量服务。
"""
import json
import uuid as uuid_lib
from unittest.mock import patch

import pytest
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken

from apps.graph.models import GraphEntity, GraphRelation, GraphCommunity
from apps.knowledge.models import KnowledgeNode, Document, VisibilityLevel
from apps.users.models import User, Role, UserRoleRel, Department, Team, GrantStatus


# ============================================================================
# 测试辅助函数
# ============================================================================
def _get_or_create_role(role_key, **defaults):
    """获取或创建内置角色，补齐默认字段"""
    default_map = {
        'super_admin': dict(name='超级管理员', is_builtin=True),
        'viewer': dict(name='查看者', is_builtin=True),
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


def _create_entity(name, doc_ids, etype='CONCEPT'):
    """创建图谱实体（直接指定来源文档 ID，绕过抽取管线）"""
    return GraphEntity.objects.create(
        name=name, type=etype, description=f'{name} 的描述',
        source_doc_ids=list(doc_ids))


# ============================================================================
# 测试基类
# ============================================================================
@pytest.mark.django_db
class GraphViewsTestBase:
    """图谱接口测试公共基类

    组织结构：研发部 → 后端组 → 业务分类节点 / 机密分类节点
    文档矩阵：
    - doc_visible：normal_user 自己的 TEAM_ONLY 文档（normal_user 可读）
    - doc_public：超管的 PUBLIC 文档（全员可读）
    - doc_hidden：超管的 TEAM_ONLY 文档（normal_user 不可读，挂在机密节点）
    图谱矩阵：
    - entity_visible：来源 [doc_visible, doc_public]，normal_user 可见
    - entity_hidden：来源 [doc_hidden]，normal_user 不可见
    - relation：entity_visible --[协作]--> entity_hidden
    - community_visible：含 entity_visible；community_hidden：含 entity_hidden
    """

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入 client/组织架构/节点树/文档/图谱数据（DB 每测试隔离）"""
        self.client = Client()
        _get_or_create_role('viewer')

        self.super_admin = _create_test_user('admin', is_super_admin=True)
        self.normal_user = _create_test_user('normal')

        # 组织架构
        self.dept = Department.objects.create(name='研发部', code='rd')
        self.team = Team.objects.create(
            name='后端组', code='rd-backend', department=self.dept)

        # 知识节点树：root → dept → team → 业务分类 + 机密分类
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
        self.doc_visible = _create_document(
            self.category_node, self.normal_user,
            visibility_level=VisibilityLevel.TEAM_ONLY,
            team_id=self.team.id, dept_id=self.dept.id, title='我的私有文档')
        self.doc_public = _create_document(
            self.category_node, self.super_admin,
            visibility_level=VisibilityLevel.PUBLIC,
            team_id=self.team.id, dept_id=self.dept.id, title='公开文档')
        self.doc_hidden = _create_document(
            self.other_node, self.super_admin,
            visibility_level=VisibilityLevel.TEAM_ONLY,
            team_id=self.team.id, dept_id=self.dept.id, title='机密文档')

        # 图谱矩阵
        self.entity_visible = _create_entity(
            '年度目标', [self.doc_visible.id, self.doc_public.id], etype='CONCEPT')
        self.entity_hidden = _create_entity(
            '机密项目', [self.doc_hidden.id], etype='PRODUCT')
        self.relation = GraphRelation.objects.create(
            source_entity=self.entity_visible,
            target_entity=self.entity_hidden,
            relation_type='协作',
            weight=1.0,
            source_doc_ids=[self.doc_public.id],
        )
        self.community_visible = GraphCommunity.objects.create(
            community_id=0, level=0,
            entity_ids=[self.entity_visible.id],
            summary='可见社区摘要',
            metadata={'topic': '业务目标'},
        )
        self.community_hidden = GraphCommunity.objects.create(
            community_id=1, level=0,
            entity_ids=[self.entity_hidden.id],
            summary='机密社区摘要',
            metadata={'topic': '机密领域'},
        )

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


# ============================================================================
# 认证拦截
# ============================================================================
class TestGraphAuthRequired(GraphViewsTestBase):
    """匿名访问应被认证拦截"""

    @pytest.mark.integration
    def test_entity_list_anonymous_401(self):
        """匿名访问实体列表应 401/403"""
        resp = self.client.get('/api/v1/graph/entities/')
        assert resp.status_code in (401, 403)

    @pytest.mark.integration
    def test_community_detect_anonymous_401(self):
        """匿名触发社区检测应 401/403"""
        resp = self.client.post('/api/v1/graph/communities/detect/',
                                data=json.dumps({}),
                                content_type='application/json')
        assert resp.status_code in (401, 403)


# ============================================================================
# 实体列表
# ============================================================================
class TestEntityList(GraphViewsTestBase):
    """实体列表权限过滤与检索"""

    @pytest.mark.integration
    def test_list_filters_by_doc_access(self):
        """普通用户仅看到来源文档可读的实体，超管看到全部"""
        resp = self.client.get('/api/v1/graph/entities/', **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        ids = [r['id'] for r in data['results']]
        assert self.entity_visible.id in ids
        assert self.entity_hidden.id not in ids
        assert data['count'] == 1

        resp_admin = self.client.get('/api/v1/graph/entities/', **_auth_headers(self.super_admin))
        assert resp_admin.status_code == 200
        admin_ids = [r['id'] for r in resp_admin.json()['results']]
        assert self.entity_visible.id in admin_ids
        assert self.entity_hidden.id in admin_ids

    @pytest.mark.integration
    def test_list_q_filter(self):
        """名称模糊过滤后仍做权限过滤"""
        resp = self.client.get(
            f'/api/v1/graph/entities/?q=机密', **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        ids = [r['id'] for r in data['results']]
        assert self.entity_hidden.id not in ids
        assert data['count'] == 0

        resp_admin = self.client.get(
            f'/api/v1/graph/entities/?q=机密', **_auth_headers(self.super_admin))
        admin_ids = [r['id'] for r in resp_admin.json()['results']]
        assert self.entity_hidden.id in admin_ids

    @pytest.mark.integration
    def test_list_type_filter(self):
        """按实体类型过滤"""
        resp = self.client.get(
            '/api/v1/graph/entities/?type=CONCEPT', **_auth_headers(self.super_admin))
        data = resp.json()
        ids = [r['id'] for r in data['results']]
        assert self.entity_visible.id in ids
        assert all(r['type'] == 'CONCEPT' for r in data['results'])


# ============================================================================
# 实体详情
# ============================================================================
class TestEntityRetrieve(GraphViewsTestBase):
    """实体详情权限判定"""

    @pytest.mark.integration
    def test_retrieve_accessible_returns_source_docs(self):
        """可读实体返回详情与可见来源文档（不含不可读文档标题）"""
        resp = self.client.get(
            f'/api/v1/graph/entities/{self.entity_visible.id}/', **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data['name'] == '年度目标'
        assert data['type_label'] == '概念'
        # 可见来源文档 = doc_visible（本人 Owner）+ doc_public（PUBLIC）
        doc_ids = {d['id'] for d in data['source_docs']}
        assert self.doc_visible.id in doc_ids
        assert self.doc_public.id in doc_ids
        assert data['source_doc_count'] == 2

    @pytest.mark.integration
    def test_retrieve_inaccessible_403(self):
        """来源文档全不可读的实体应 403"""
        resp = self.client.get(
            f'/api/v1/graph/entities/{self.entity_hidden.id}/', **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_retrieve_not_found_404(self):
        """不存在的实体应 404"""
        resp = self.client.get('/api/v1/graph/entities/99999/',
                               **_auth_headers(self.super_admin))
        assert resp.status_code == 404


# ============================================================================
# 语义检索
# ============================================================================
class TestEntitySearch(GraphViewsTestBase):
    """语义向量检索 + 权限过滤（mock 向量生成与召回）"""

    def _mock_hits(self):
        """模拟向量召回：两个实体都命中，得分一高一低"""
        return [
            {'entity_id': self.entity_visible.id, 'name': '年度目标',
             'type': 'CONCEPT', 'description': 'desc', 'score': 0.8},
            {'entity_id': self.entity_hidden.id, 'name': '机密项目',
             'type': 'PRODUCT', 'description': 'desc', 'score': 0.6},
        ]

    @pytest.mark.integration
    @patch('apps.graph.views.search_entities')
    @patch('apps.graph.views.get_embedding_client')
    def test_search_filters_by_doc_access(self, mock_embed, mock_search):
        """向量命中但无来源权限的实体不应出现在检索结果中"""
        mock_embed.return_value.embed_one.return_value = [0.1] * 1024
        mock_search.return_value = self._mock_hits()

        resp = self.client.get(
            '/api/v1/graph/entities/search/?q=目标', **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        ids = [r['entity_id'] for r in resp.json()['results']]
        assert self.entity_visible.id in ids
        assert self.entity_hidden.id not in ids

        # 超管可看到全部命中
        resp_admin = self.client.get(
            '/api/v1/graph/entities/search/?q=目标', **_auth_headers(self.super_admin))
        admin_ids = [r['entity_id'] for r in resp_admin.json()['results']]
        assert self.entity_visible.id in admin_ids
        assert self.entity_hidden.id in admin_ids

    @pytest.mark.integration
    @patch('apps.graph.views.search_entities')
    @patch('apps.graph.views.get_embedding_client')
    def test_search_type_filter_passed(self, mock_embed, mock_search):
        """type 过滤参数应透传到向量检索"""
        mock_embed.return_value.embed_one.return_value = [0.1] * 1024
        mock_search.return_value = self._mock_hits()

        resp = self.client.get(
            '/api/v1/graph/entities/search/?q=目标&type=CONCEPT', **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        mock_search.assert_called_once()
        # entity_types=[CONCEPT] 传入向量检索
        assert mock_search.call_args.kwargs['entity_types'] == ['CONCEPT']

    @pytest.mark.integration
    def test_search_requires_q(self):
        """缺少 q 参数应 400"""
        resp = self.client.get('/api/v1/graph/entities/search/',
                               **_auth_headers(self.super_admin))
        assert resp.status_code == 400

    @pytest.mark.integration
    @patch('apps.graph.views.get_embedding_client')
    def test_search_zero_vector_returns_empty(self, mock_embed):
        """向量全零（embedding 服务异常）应返回空结果而非报错"""
        mock_embed.return_value.embed_one.return_value = [0.0] * 1024
        resp = self.client.get(
            '/api/v1/graph/entities/search/?q=目标', **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        assert resp.json()['results'] == []


# ============================================================================
# 邻居子图
# ============================================================================
class TestEntityNeighbors(GraphViewsTestBase):
    """邻居子图扩展与权限过滤"""

    @pytest.mark.integration
    def test_neighbors_regular_user_edges_filtered(self):
        """关系仅当两端实体均可见时返回：普通用户看不到指向不可见实体的边"""
        resp = self.client.get(
            f'/api/v1/graph/entities/{self.entity_visible.id}/neighbors/?depth=2',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        node_ids = {n['id'] for n in data['nodes']}
        assert self.entity_visible.id in node_ids
        assert self.entity_hidden.id not in node_ids
        # 边两端必须均可见，普通用户应看不到该边
        assert data['edges'] == []

    @pytest.mark.integration
    def test_neighbors_super_admin_sees_subgraph(self):
        """超管可见完整子图（两端均可见的边）"""
        resp = self.client.get(
            f'/api/v1/graph/entities/{self.entity_visible.id}/neighbors/?depth=2',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        data = resp.json()
        node_ids = {n['id'] for n in data['nodes']}
        assert self.entity_visible.id in node_ids
        assert self.entity_hidden.id in node_ids
        assert data['center'] == self.entity_visible.id
        assert len(data['edges']) == 1
        assert data['edges'][0]['relation_type'] == '协作'

    @pytest.mark.integration
    def test_neighbors_depth_limit(self):
        """depth=1 只扩展一跳，depth=2 扩展两跳（链式 A-B-C）"""
        doc = _create_document(
            self.category_node, self.super_admin,
            visibility_level=VisibilityLevel.PUBLIC,
            team_id=self.team.id, dept_id=self.dept.id, title='链式文档')
        a = _create_entity('A', [doc.id])
        b = _create_entity('B', [doc.id])
        c = _create_entity('C', [doc.id])
        GraphRelation.objects.create(source_entity=a, target_entity=b, relation_type='指向')
        GraphRelation.objects.create(source_entity=b, target_entity=c, relation_type='指向')

        resp1 = self.client.get(
            f'/api/v1/graph/entities/{a.id}/neighbors/?depth=1', **_auth_headers(self.super_admin))
        ids1 = {n['id'] for n in resp1.json()['nodes']}
        assert ids1 == {a.id, b.id}

        resp2 = self.client.get(
            f'/api/v1/graph/entities/{a.id}/neighbors/?depth=2', **_auth_headers(self.super_admin))
        ids2 = {n['id'] for n in resp2.json()['nodes']}
        assert ids2 == {a.id, b.id, c.id}

    @pytest.mark.integration
    def test_neighbors_inaccessible_403(self):
        """不可见实体不提供邻居扩展入口"""
        resp = self.client.get(
            f'/api/v1/graph/entities/{self.entity_hidden.id}/neighbors/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403


# ============================================================================
# 社区列表 / 详情
# ============================================================================
class TestCommunityList(GraphViewsTestBase):
    """社区列表权限过滤"""

    @pytest.mark.integration
    def test_list_filters_by_entity_access(self):
        """普通用户仅看到含可见实体的社区，超管看到全部"""
        resp = self.client.get('/api/v1/graph/communities/', **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        ids = [r['id'] for r in data['results']]
        assert self.community_visible.id in ids
        assert self.community_hidden.id not in ids
        assert data['count'] == 1

        resp_admin = self.client.get('/api/v1/graph/communities/', **_auth_headers(self.super_admin))
        admin_ids = [r['id'] for r in resp_admin.json()['results']]
        assert self.community_visible.id in admin_ids
        assert self.community_hidden.id in admin_ids

    @pytest.mark.integration
    def test_list_level_filter(self):
        """按粒度过滤社区"""
        resp = self.client.get(
            '/api/v1/graph/communities/?level=1', **_auth_headers(self.super_admin))
        data = resp.json()
        assert all(r['level'] == 1 for r in data['results'])

    @pytest.mark.integration
    def test_list_search_by_keyword(self):
        """q 按主题/摘要关键词过滤社区，且权限过滤仍生效"""
        resp = self.client.get(
            '/api/v1/graph/communities/?q=业务目标', **_auth_headers(self.normal_user))
        data = resp.json()
        ids = [r['id'] for r in data['results']]
        assert self.community_visible.id in ids
        assert self.community_hidden.id not in ids
        assert data['count'] == 1

        resp_empty = self.client.get(
            '/api/v1/graph/communities/?q=不存在的主题', **_auth_headers(self.super_admin))
        assert resp_empty.json()['count'] == 0

        resp_admin = self.client.get(
            '/api/v1/graph/communities/?q=机密领域', **_auth_headers(self.super_admin))
        assert self.community_hidden.id in [r['id'] for r in resp_admin.json()['results']]


class TestCommunityRetrieve(GraphViewsTestBase):
    """社区详情权限判定"""

    @pytest.mark.integration
    def test_retrieve_accessible_returns_visible_entities(self):
        """可读社区返回详情，实体列表仅含可见实体"""
        resp = self.client.get(
            f'/api/v1/graph/communities/{self.community_visible.id}/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data['topic'] == '业务目标'
        assert data['summary'] == '可见社区摘要'
        entity_ids = [e['id'] for e in data['entities']]
        assert self.entity_visible.id in entity_ids
        assert self.entity_hidden.id not in entity_ids

    @pytest.mark.integration
    def test_retrieve_inaccessible_403(self):
        """含不可见实体的社区对普通用户应 403"""
        resp = self.client.get(
            f'/api/v1/graph/communities/{self.community_hidden.id}/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403


# ============================================================================
# 手动触发社区检测
# ============================================================================
class TestCommunityDetect(GraphViewsTestBase):
    """社区检测触发权限"""

    @pytest.mark.integration
    @patch('apps.graph.tasks.community_detection_task')
    def test_detect_requires_admin(self, mock_task):
        """普通用户触发应 403，不提交任务"""
        resp = self.client.post('/api/v1/graph/communities/detect/',
                                data=json.dumps({}),
                                content_type='application/json',
                                **_auth_headers(self.normal_user))
        assert resp.status_code == 403
        mock_task.delay.assert_not_called()

    @pytest.mark.integration
    @patch('apps.graph.tasks.community_detection_task')
    def test_detect_admin_triggers_task(self, mock_task):
        """超管触发应 200 并提交 community_detection_task"""
        resp = self.client.post('/api/v1/graph/communities/detect/',
                                data=json.dumps({}),
                                content_type='application/json',
                                **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        data = resp.json()
        assert data['ok'] is True
        mock_task.delay.assert_called_once()
