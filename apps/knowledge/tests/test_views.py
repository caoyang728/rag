"""
apps.knowledge.views 接口集成测试 —— 节点树 & 文档 CRUD API 端点

覆盖范围：
- 认证拦截：匿名访问文档/节点接口应 401/403
- DocumentViewSet：list（按可见范围过滤）/ retrieve（can_read 校验）
  / update（写权限）/ destroy（软删 + 置灰向量/图谱清理）
- KnowledgeNodeViewSet：list（需 kb.node.manage）/ retrieve（登录即可）/ tree 视图
- DocumentUploadView：上传鉴权与必填校验（mock _save_file / magic / parse_document）
- DocumentChunksView：切片查看的 can_read 校验

采用 pytest-django（django_db）+ JWT：
接口涉及 RBAC 权限判定（resolve_doc_access）、ORM 写入与软删、
节点树组织归属推导，需真实 DB + 真实权限链路验证端到端契约，
mock 外部依赖（向量库 / 图谱 / Celery / libmagic）避免基础设施耦合。
"""
import json
import uuid as uuid_lib
from unittest.mock import patch, MagicMock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken

from apps.knowledge.models import (
    KnowledgeNode, Document, DocumentChunk, ResourceShare, ResourceBlockList,
    VisibilityLevel, ResourceType, ShareScopeType, AccessLevel, ShareStatus,
)
from apps.knowledge.views import (
    _normalize_visibility_level, _encode_ticket_reason, _decode_ticket_reason,
    _extract_last_comment, _detect_file_type, _build_tree, _get_user_role,
    DocumentUploadView,
)
from apps.users.models import (
    User, Role, UserRoleRel, GrantStatus, Department, Team,
    Permission, RolePermissionRel,
    PermissionApprovalTicket, TicketStatus, TicketChangeType,
)


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
    """创建测试用户，is_super_admin 时授予 super_admin 角色

    is_super_admin 是 User 模型的 @property（基于 super_admin 角色判定），
    故必须通过 UserRoleRel 授予角色，不能直接写字段。
    """
    user = User.objects.create_user(
        username=username, password=password, email=f'{username}@test.com')
    if is_super_admin:
        admin_role = _get_or_create_role('super_admin')
        UserRoleRel.objects.get_or_create(
            user=user, role=admin_role,
            defaults={'status': GrantStatus.ACTIVE})
    return user


def _get_auth_token(user):
    """生成 JWT access token"""
    return str(RefreshToken.for_user(user).access_token)


def _auth_headers(user):
    """构造 JWT 认证 header"""
    return {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(user)}'}


def _create_document(node, owner, visibility_level=VisibilityLevel.TEAM_ONLY,
                     team_id=None, dept_id=None, title='测试文档',
                     file_name=None, **extra):
    """创建文档记录（直接 ORM 写入，绕过上传管线）

    file_hash 用 uuid 保证唯一，避免同节点同名同版本唯一约束冲突。
    归属约束：team_id 或 dept_id 至少一个非空。
    extra 可覆盖默认字段（如 status='pending' 构造待处理文档）。
    """
    fields = {
        'node': node,
        'title': title,
        'file_name': file_name or f'{title}.txt',
        'file_type': 'txt',
        'file_size': 100,
        'file_hash': uuid_lib.uuid4().hex,
        'file_path': '/tmp/fake.txt',
        'mime_type': 'text/plain',
        'owner': owner,
        'dept_id': dept_id,
        'team_id': team_id,
        'visibility_level': visibility_level,
        'root_type': node.root_type,
        'status': 'done',
    }
    fields.update(extra)
    return Document.objects.create(**fields)


# ============================================================================
# 测试基类：准备组织架构 + 知识节点树 + 多可见性文档
# ============================================================================
@pytest.mark.django_db
class KnowledgeViewsTestBase:
    """知识库接口测试公共基类

    组织结构：研发部 → 后端组 → 业务分类节点
    文档矩阵：
    - doc_own_private：normal_user 自己的 TEAM_ONLY 文档（对其可见）
    - doc_other_public：超管的 PUBLIC 文档（对所有人可见）
    - doc_other_private：超管的 TEAM_ONLY 文档（对 normal_user 不可见）
    """

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入 client/组织架构/节点树/文档矩阵（DB 每测试隔离）"""
        self._init_env()

    def _init_env(self):
        """构造测试环境：client/角色/用户/组织架构/节点树/文档矩阵（供子类复用）"""
        self.client = Client()
        _get_or_create_role('viewer')

        self.super_admin = _create_test_user('admin', is_super_admin=True)
        self.normal_user = _create_test_user('normal')

        # 组织架构
        self.dept = Department.objects.create(name='研发部', code='rd')
        self.team = Team.objects.create(
            name='后端组', code='rd-backend', department=self.dept)

        # 知识节点树：root → dept → team → 业务分类
        self.root_node = self._create_node('知识库', 'root', node_level=1)
        self.dept_node = self._create_node(
            '研发部', 'folder', node_level=2, parent=self.root_node,
            ref_id=self.dept.id)
        self.team_node = self._create_node(
            '后端组', 'folder', node_level=3, parent=self.dept_node,
            ref_id=self.team.id)
        self.category_node = self._create_node(
            '业务分类', 'folder', node_level=4, parent=self.team_node)

        # 文档矩阵
        self.doc_own_private = _create_document(
            self.category_node, self.normal_user,
            visibility_level=VisibilityLevel.TEAM_ONLY,
            team_id=self.team.id, dept_id=self.dept.id,
            title='我的私有文档', file_name='own_private.txt')
        self.doc_other_public = _create_document(
            self.category_node, self.super_admin,
            visibility_level=VisibilityLevel.PUBLIC,
            team_id=self.team.id, dept_id=self.dept.id,
            title='他人公开文档', file_name='other_public.txt')
        self.doc_other_private = _create_document(
            self.category_node, self.super_admin,
            visibility_level=VisibilityLevel.TEAM_ONLY,
            team_id=self.team.id, dept_id=self.dept.id,
            title='他人私有文档', file_name='other_private.txt')

    def _create_node(self, name, node_type, node_level, parent=None, ref_id=None):
        """创建节点并回填 path（路径枚举 /id1/id2/.../，4 位零填充）

        node_kind 按层级自动推导：1=ROOT / 2-3=ORG / 4+=FOLDER，与 node_sync 同步逻辑保持一致。
        """
        if node_level == 1:
            node_kind = 'ROOT'
        elif node_level in (2, 3):
            node_kind = 'ORG'
        else:
            node_kind = 'FOLDER'
        node = KnowledgeNode.objects.create(
            name=name, node_type=node_type, node_level=node_level,
            node_kind=node_kind,
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


@pytest.mark.django_db
class KnowledgeViewsExtraBase:
    """知识库接口补充测试公共基类（供子域补充测试文件复用）

    组织架构：研发部 → 后端组（leader=team_leader_user）→ 业务分类节点
    文档矩阵：
    - doc_own_private：normal_user 自己的 TEAM_ONLY 文档（对其可见/可写）
    - doc_other_public：超管的 PUBLIC 文档（所有人可读，仅超管可写）
    - doc_other_private：超管的 TEAM_ONLY 文档（对 normal_user 不可见）
    """

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入 client/组织架构/节点树/文档矩阵（DB 每测试隔离）"""
        self._init_env()

    def _init_env(self):
        """构造测试环境：client/角色/用户/组织架构/节点树/文档矩阵（供子类复用）"""
        self.client = Client()
        _get_or_create_role('viewer')

        self.super_admin = _create_test_user('admin', is_super_admin=True)
        self.admin2 = _create_test_user('admin2', is_super_admin=True)
        self.normal_user = _create_test_user('normal')
        self.other_user = _create_test_user('other')
        self.team_leader = _create_test_user('leader')

        # 组织架构
        self.dept = Department.objects.create(name='研发部', code='rd')
        self.team = Team.objects.create(
            name='后端组', code='rd-backend', department=self.dept,
            leader=self.team_leader)
        # 团队组长：补全组织归属 + user.manage 权限，使 _get_user_role 能识别 team_leader
        self.team_leader.team_id = self.team.id
        self.team_leader.save(update_fields=['team_id'])
        leader_role = _get_or_create_role('team_leader')
        leader_perm, _ = Permission.objects.get_or_create(
            permission_key='user.manage',
            defaults={'permission_name': '用户管理', 'module': 'user'})
        RolePermissionRel.objects.get_or_create(
            role=leader_role, permission=leader_perm,
            defaults={'granted_by': self.super_admin, 'is_active': True})
        UserRoleRel.objects.get_or_create(
            user=self.team_leader, role=leader_role,
            defaults={'status': GrantStatus.ACTIVE})

        # 知识节点树：root → dept → team → 业务分类
        self.root_node = self._create_node('知识库', 'root', node_level=1)
        self.dept_node = self._create_node(
            '研发部', 'folder', node_level=2, parent=self.root_node,
            ref_id=self.dept.id)
        self.team_node = self._create_node(
            '后端组', 'folder', node_level=3, parent=self.dept_node,
            ref_id=self.team.id)
        self.category_node = self._create_node(
            '业务分类', 'folder', node_level=4, parent=self.team_node)

        # 文档矩阵
        self.doc_own_private = _create_document(
            self.category_node, self.normal_user,
            visibility_level=VisibilityLevel.TEAM_ONLY,
            team_id=self.team.id, dept_id=self.dept.id,
            title='我的私有文档', file_name='own_private.txt')
        self.doc_other_public = _create_document(
            self.category_node, self.super_admin,
            visibility_level=VisibilityLevel.PUBLIC,
            team_id=self.team.id, dept_id=self.dept.id,
            title='他人公开文档', file_name='other_public.txt')
        self.doc_other_private = _create_document(
            self.category_node, self.super_admin,
            visibility_level=VisibilityLevel.TEAM_ONLY,
            team_id=self.team.id, dept_id=self.dept.id,
            title='他人私有文档', file_name='other_private.txt')

    def _create_node(self, name, node_type, node_level, parent=None, ref_id=None):
        """创建节点并回填 path（/id1/id2/.../，4 位零填充）

        node_kind 按层级自动推导：1=ROOT / 2-3=ORG / 4+=FOLDER，与 node_sync 同步逻辑保持一致。
        """
        if node_level == 1:
            node_kind = 'ROOT'
        elif node_level in (2, 3):
            node_kind = 'ORG'
        else:
            node_kind = 'FOLDER'
        node = KnowledgeNode.objects.create(
            name=name, node_type=node_type, node_level=node_level,
            node_kind=node_kind,
            root_type='company_doc', parent=parent, ref_id=ref_id,
            depth=(parent.depth + 1) if parent else 0,
            created_by=self.super_admin,
        )
        padded = f'{node.id:04d}'
        node.path = f'{parent.path}{padded}/' if parent else f'/{padded}/'
        node.save(update_fields=['path'])
        return node

    def _request_ticket(self, applicant, doc, action='read', reason='测试申请'):
        """直接 ORM 创建一条文档访问申请工单（模拟 request_access 产物）"""
        return PermissionApprovalTicket.objects.create(
            ticket_no=f'DOC-REQ-{uuid_lib.uuid4().hex[:12].upper()}',
            applicant=applicant,
            target_user=applicant,
            change_type=TicketChangeType.GRANT,
            reason=_encode_ticket_reason('doc', doc.id, action, reason),
            status=TicketStatus.PENDING,
            approval_chain=[
                {'step': 0, 'approver_id': None, 'status': 'pending',
                 'comment': '', 'approved_at': None},
            ],
            current_step=0,
        )


# ============================================================================
# 认证拦截
# ============================================================================
class TestAuthenticationRequired(KnowledgeViewsTestBase):
    """匿名访问应被认证拦截"""

    @pytest.mark.integration
    def test_document_list_anonymous_401(self):
        """匿名访问文档列表应 401/403"""
        resp = self.client.get('/api/v1/knowledge/documents/')
        assert resp.status_code in (401, 403)

    @pytest.mark.integration
    def test_node_tree_anonymous_401(self):
        """匿名访问节点树应 401/403"""
        resp = self.client.get('/api/v1/knowledge/nodes/tree/')
        assert resp.status_code in (401, 403)

    @pytest.mark.integration
    def test_upload_anonymous_401(self):
        """匿名上传应 401/403"""
        resp = self.client.post('/api/v1/knowledge/documents/upload/')
        assert resp.status_code in (401, 403)


# ============================================================================
# 文档列表（按可见范围过滤）
# ============================================================================
class TestDocumentList(KnowledgeViewsTestBase):
    """DocumentViewSet.list 可见范围过滤测试"""

    @pytest.mark.integration
    def test_normal_user_sees_own_and_public(self):
        """普通用户仅能看到自己的文档 + 全局公开文档

        非管理员的 get_queryset 限制为 Q(owner=user) | Q(PUBLIC)，
        跨团队/跨部门的文档需通过 ResourceShare 共享才能可见。
        """
        resp = self.client.get('/api/v1/knowledge/documents/',
                               **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        titles = {d['title'] for d in self._results(resp)}
        assert '我的私有文档' in titles        # 自己的
        assert '他人公开文档' in titles        # PUBLIC
        assert '他人私有文档' not in titles    # 不可见

    @pytest.mark.integration
    def test_super_admin_sees_all(self):
        """超管能看到全部文档（系统级快路径，绕过可见范围过滤）"""
        resp = self.client.get('/api/v1/knowledge/documents/',
                               **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        titles = {d['title'] for d in self._results(resp)}
        assert '我的私有文档' in titles
        assert '他人公开文档' in titles
        assert '他人私有文档' in titles


# ============================================================================
# 文档详情（can_read 校验）
# ============================================================================
class TestDocumentRetrieve(KnowledgeViewsTestBase):
    """DocumentViewSet.retrieve 权限校验测试"""

    @pytest.mark.integration
    def test_retrieve_owner_200(self):
        """Owner 可获取自己的文档详情"""
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert resp.json()['title'] == '我的私有文档'

    @pytest.mark.integration
    def test_retrieve_public_200(self):
        """任意已登录用户可获取 PUBLIC 文档"""
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_other_public.id}/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert resp.json()['can_read'] is True

    @pytest.mark.integration
    def test_retrieve_other_private_404(self):
        """普通用户检索他人 TEAM_ONLY 文档：get_queryset 已过滤 → 404

        非管理员 queryset 限制为 owner+PUBLIC，
        他人私有文档不在 queryset 内，get_object 直接 404，避免泄露存在性。
        """
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_other_private.id}/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_retrieve_other_private_via_discover_403(self):
        """discover 模式下他人私有文档进入 queryset，但 can_read 校验拒绝 → 403

        发现模式返回全部文档用于浏览与申请权限，
        但 get_object 的 _access 仍按 resolve_doc_access 判定 can_read，
        无共享/无可见范围 → 403，引导用户走申请流程。
        """
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_other_private.id}/?discover=true',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403


# ============================================================================
# 文档更新（写权限校验）
# ============================================================================
class TestDocumentUpdate(KnowledgeViewsTestBase):
    """DocumentViewSet.perform_update 写权限测试"""

    @pytest.mark.integration
    def test_update_owner_200(self):
        """Owner 修改非可见性字段（如 title）应成功"""
        resp = self.client.patch(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/',
            data=json.dumps({'title': '改名后的文档'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        self.doc_own_private.refresh_from_db()
        assert self.doc_own_private.title == '改名后的文档'

    @pytest.mark.integration
    def test_update_non_owner_public_403(self):
        """非 Owner 修改他人 PUBLIC 文档应 403（can_read 但非 owner/manager）

        可见范围只授予读权限，
        写操作（_require_write）要求 is_owner 或 is_manager，防止越权篡改。
        """
        resp = self.client.patch(
            f'/api/v1/knowledge/documents/{self.doc_other_public.id}/',
            data=json.dumps({'title': '恶意改名'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403


# ============================================================================
# 文档删除（软删 + 外部清理）
# ============================================================================
class TestDocumentDelete(KnowledgeViewsTestBase):
    """DocumentViewSet.destroy 软删与外部依赖清理测试"""

    @pytest.mark.integration
    @patch('apps.graph.sync.on_document_deleted')
    @patch('apps.retrieval.vector_store.delete_by_document')
    def test_delete_owner_soft_204(self, _mock_vec, _mock_graph):
        """Owner 删除文档：软删（is_deleted=True）+ 触发向量/图谱清理 + 204

        删除联动向量库与图谱属于外部依赖，
        测试关注软删主流程与审计，外部清理只需验证被调用。
        """
        resp = self.client.delete(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 204
        self.doc_own_private.refresh_from_db()
        assert self.doc_own_private.is_deleted is True
        assert self.doc_own_private.delete_time is not None
        # 外部依赖应被调用清理
        _mock_vec.assert_called_once_with(self.doc_own_private.id)
        _mock_graph.assert_called_once_with(self.doc_own_private.id)

    @pytest.mark.integration
    @patch('apps.graph.sync.on_document_deleted')
    @patch('apps.retrieval.vector_store.delete_by_document')
    def test_delete_non_owner_public_403(self, _mock_vec, _mock_graph):
        """非 Owner 删除他人 PUBLIC 文档应 403（_require_write 拒绝）"""
        resp = self.client.delete(
            f'/api/v1/knowledge/documents/{self.doc_other_public.id}/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403
        # 被拒后不应触发任何清理
        _mock_vec.assert_not_called()
        _mock_graph.assert_not_called()


# ============================================================================
# 知识节点 list / retrieve / tree
# ============================================================================
class TestKnowledgeNode(KnowledgeViewsTestBase):
    """KnowledgeNodeViewSet + NodeTreeView 测试"""

    @pytest.mark.integration
    def test_node_tree_authenticated_200(self):
        """已登录用户可获取节点树（NodeTreeView 仅需 IsAuthenticated）"""
        resp = self.client.get('/api/v1/knowledge/nodes/tree/',
                               **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert 'tree' in data
        assert data['total'] >= 4  # root + dept + team + category

    @pytest.mark.integration
    def test_node_retrieve_authenticated_200(self):
        """已登录用户可获取单个节点详情（retrieve 仅需 IsAuthenticated）"""
        resp = self.client.get(
            f'/api/v1/knowledge/nodes/{self.category_node.id}/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert resp.json()['name'] == '业务分类'

    @pytest.mark.integration
    def test_node_list_super_admin_200(self):
        """超管可列出全部节点（list 需 IsAdminOrOps）"""
        resp = self.client.get('/api/v1/knowledge/nodes/',
                               **_auth_headers(self.super_admin))
        assert resp.status_code == 200

    @pytest.mark.integration
    def test_node_list_normal_user_403(self):
        """普通用户无 kb.node.manage 权限 → list 应 403"""
        resp = self.client.get('/api/v1/knowledge/nodes/',
                               **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_node_root_types_authenticated_200(self):
        """已登录用户可获取根类型列表"""
        resp = self.client.get('/api/v1/knowledge/nodes/root_types/',
                               **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert 'root_types' in resp.json()


# ============================================================================
# 文档上传（mock 外部依赖）
# ============================================================================
class TestDocumentUpload(KnowledgeViewsTestBase):
    """DocumentUploadView 上传测试（mock 文件存储/libmagic/Celery）"""

    @pytest.mark.integration
    def test_upload_missing_file_400(self):
        """已登录但缺少 file/node_id 应 400"""
        resp = self.client.post(
            '/api/v1/knowledge/documents/upload/',
            data={'node_id': self.category_node.id},
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400

    @pytest.mark.integration
    @patch('apps.knowledge.tasks.parse_document')
    @patch('apps.knowledge.views.magic')
    @patch.object(DocumentUploadView, '_save_file',
                  return_value='/tmp/uploaded.txt')
    def test_upload_success_201(self, _mock_save, mock_magic, mock_parse):
        """超管上传 txt 文件应成功创建文档并触发解析任务

        mock 的三个依赖：
        - _save_file：避免真实磁盘写入
        - magic.from_buffer：避免依赖系统 libmagic 版本差异
        - parse_document.delay：避免 Celery broker 连接
        """
        # libmagic 检测返回与 .txt 扩展名一致的 MIME
        mock_magic.from_buffer.return_value = 'text/plain'
        upload = SimpleUploadedFile('新文档.txt', b'hello world',
                                    content_type='text/plain')

        resp = self.client.post(
            '/api/v1/knowledge/documents/upload/',
            data={
                'file': upload,
                'node_id': self.category_node.id,
                'visibility_level': 'TEAM_ONLY',
            },
            **_auth_headers(self.super_admin))

        assert resp.status_code == 201
        data = resp.json()
        assert data['celery_ok'] is True
        assert Document.objects.filter(id=data['document_id'],
                                       is_deleted=False).exists()
        # 解析任务应被派发
        mock_parse.delay.assert_called_once()


# ============================================================================
# 文档切片查看（can_read 校验）
# ============================================================================
class TestDocumentChunks(KnowledgeViewsTestBase):
    """DocumentChunksView 切片查看测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """在基类环境基础上补充文档切片（DB 每测试隔离）"""
        self._init_env()
        # 为 own_private 文档创建 2 个切片
        DocumentChunk.objects.create(
            document=self.doc_own_private, chunk_index=0,
            chunk_type='text', content='第一段切片')
        DocumentChunk.objects.create(
            document=self.doc_own_private, chunk_index=1,
            chunk_type='text', content='第二段切片')

    @pytest.mark.integration
    def test_chunks_owner_200(self):
        """Owner 可查看自己文档的切片"""
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/chunks/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 2
        assert data['document_id'] == self.doc_own_private.id

    @pytest.mark.integration
    def test_chunks_no_permission_403(self):
        """非 Owner 查看他人 TEAM_ONLY 文档切片应 403

        DocumentChunksView 直接按 id 取文档，
        不经 queryset 过滤，文档存在但 can_read=False → 403。
        """
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_other_private.id}/chunks/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_chunks_public_200(self):
        """任意已登录用户可查看 PUBLIC 文档切片"""
        DocumentChunk.objects.create(
            document=self.doc_other_public, chunk_index=0,
            chunk_type='text', content='公开切片')
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_other_public.id}/chunks/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert resp.json()['total'] == 1


# ============================================================================
# 纯函数单元测试（无 DB）
# ============================================================================
class TestViewHelpers:
    """views 模块纯函数测试"""

    @pytest.mark.unit
    def test_normalize_visibility_level(self):
        """新版值原样透传 / 旧版 visible_scope 映射 / 非法值返回 None"""
        assert _normalize_visibility_level('TEAM_ONLY') == 'TEAM_ONLY'
        assert _normalize_visibility_level('DEPT_ONLY') == 'DEPT_ONLY'
        assert _normalize_visibility_level('PUBLIC') == 'PUBLIC'
        assert _normalize_visibility_level('team') == VisibilityLevel.TEAM_ONLY
        assert _normalize_visibility_level('dept') == VisibilityLevel.DEPT_ONLY
        assert _normalize_visibility_level('public') == VisibilityLevel.PUBLIC
        assert _normalize_visibility_level('INVALID') is None
        assert _normalize_visibility_level('') is None
        assert _normalize_visibility_level(None) is None

    @pytest.mark.unit
    def test_ticket_reason_roundtrip(self):
        """编码 → 解码往返一致（doc/node 两种前缀）"""
        reason = _encode_ticket_reason('doc', 42, 'read', '需要参考')
        assert reason == '[doc:42:read] 需要参考'
        assert _decode_ticket_reason(reason) == ('doc', 42, 'read', '需要参考')
        # 无 user_reason 时前缀后无多余空格
        assert _encode_ticket_reason('doc', 7, 'download', '') == '[doc:7:download] '
        # 节点可见范围变更工单前缀
        assert _encode_ticket_reason('node', 9, 'visibility_change', '目标值:PUBLIC') == \
            '[node:9:visibility_change] 目标值:PUBLIC'
        assert _decode_ticket_reason('[node:9:visibility_change] 目标值:PUBLIC') == \
            ('node', 9, 'visibility_change', '目标值:PUBLIC')

    @pytest.mark.unit
    def test_decode_ticket_reason_unprefixed(self):
        """非文档工单 reason 无法解析 → (None, None, None, 原文)"""
        assert _decode_ticket_reason('普通理由') == (None, None, None, '普通理由')
        assert _decode_ticket_reason('') == (None, None, None, '')
        assert _decode_ticket_reason(None) == (None, None, None, '')

    @pytest.mark.unit
    def test_extract_last_comment(self):
        """从 approval_chain 提取最近一条有内容的审批意见"""
        chain = [
            {'step': 0, 'comment': '', 'status': 'pending'},
            {'step': 1, 'comment': '同意', 'status': 'approved'},
            {'step': 2, 'comment': '复核通过', 'status': 'approved'},
        ]
        assert _extract_last_comment(chain) == '复核通过'
        # 最后一条无 comment → 向前找
        chain2 = [{'step': 0, 'comment': '', 'status': 'pending'},
                  {'step': 1, 'comment': '同意', 'status': 'approved'},
                  {'step': 2, 'comment': '', 'status': 'approved'}]
        assert _extract_last_comment(chain2) == '同意'
        assert _extract_last_comment([]) == ''
        assert _extract_last_comment(None) == ''
        assert _extract_last_comment('not-a-list') == ''

    @pytest.mark.unit
    def test_detect_file_type(self):
        """扩展名 → 文件类型映射（大小写不敏感），未知类型返回 other"""
        assert _detect_file_type('a.pdf') == 'pdf'
        assert _detect_file_type('a.PDF') == 'pdf'
        assert _detect_file_type('a.docx') == 'docx'
        assert _detect_file_type('a.py') == 'code'
        assert _detect_file_type('a.yaml') == 'config'
        assert _detect_file_type('a.xyz') == 'other'

    @pytest.mark.unit
    def test_build_tree_single_chain(self):
        """扁平列表 → 单链树"""
        nodes = [
            {'id': 1, 'parent_id': None, 'name': 'root'},
            {'id': 2, 'parent_id': 1, 'name': 'child'},
            {'id': 3, 'parent_id': 2, 'name': 'grand'},
        ]
        tree = _build_tree(nodes)
        assert len(tree) == 1
        assert tree[0]['id'] == 1
        assert tree[0]['children'][0]['id'] == 2
        assert tree[0]['children'][0]['children'][0]['id'] == 3
        # children 字段始终存在
        assert tree[0]['children'][0]['children'][0]['children'] == []

    @pytest.mark.unit
    def test_build_tree_multiple_roots(self):
        """多根节点并列"""
        nodes = [{'id': 1, 'parent_id': None}, {'id': 2, 'parent_id': None}]
        assert len(_build_tree(nodes)) == 2

    @pytest.mark.unit
    def test_build_tree_orphan_becomes_root(self):
        """父节点缺失（孤儿）时按根节点处理，不丢数据"""
        nodes = [{'id': 1, 'parent_id': 99}, {'id': 2, 'parent_id': 1}]
        tree = _build_tree(nodes)
        assert len(tree) == 1
        assert tree[0]['id'] == 1


# ============================================================================
# _get_user_role（依赖 DB，django_db 集成测试）
# ============================================================================
@pytest.mark.django_db
class TestGetUserRole:
    """_get_user_role 角色判定测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入超管/普通用户（DB 每测试隔离）"""
        self.super_admin = _create_test_user('role_admin', is_super_admin=True)
        self.normal_user = _create_test_user('role_normal')

    @pytest.mark.unit
    def test_super_admin_role(self):
        """超管 → super_admin 快路径"""
        assert _get_user_role(self.super_admin) == ('super_admin', None, [])

    @pytest.mark.unit
    def test_normal_user_no_role(self):
        """无任何角色的普通用户 → role=None（无上传/管理权限）"""
        role, dept_id, team_ids = _get_user_role(self.normal_user)
        assert role is None

    @pytest.mark.unit
    def test_anonymous_user_none(self):
        """匿名用户 → (None, None, [])"""
        anon = MagicMock()
        anon.is_authenticated = False
        assert _get_user_role(anon) == (None, None, [])


# ============================================================================
# Celery 服务状态
# ============================================================================
class TestCeleryStatus(KnowledgeViewsExtraBase):
    """CeleryStatusView 服务状态检查测试"""

    @pytest.mark.integration
    @patch('rag_project.celery.app.connection_for_read')
    def test_broker_down_returns_ok_false(self, mock_conn):
        """broker 连接失败 → celery_ok=False（200 降级响应）"""
        mock_conn.side_effect = Exception('redis down')
        resp = self.client.get(
            '/api/v1/knowledge/celery/status/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        assert resp.json()['celery_ok'] is False

    @pytest.mark.integration
    @patch('rag_project.celery.app.control.ping')
    @patch('rag_project.celery.app.connection_for_read')
    def test_ping_success_returns_ok_true(self, mock_conn, mock_ping):
        """worker ping 成功 → celery_ok=True + worker_count"""
        mock_ping.return_value = [{'worker': 'ok'}, {'worker2': 'ok'}]
        resp = self.client.get(
            '/api/v1/knowledge/celery/status/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        data = resp.json()
        assert data['celery_ok'] is True
        assert data['worker_count'] == 2
