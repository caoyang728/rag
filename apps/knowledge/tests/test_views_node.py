"""
apps.knowledge.views 节点相关补充测试 —— 可见性选项 / 节点树过滤 / 节点增删改分支

与 test_views.py 互补：
- AllowedVisibilityView：可选部门/团队，含 role 分支
- NodeTreeView：root_type 过滤
- KnowledgeNodeViewSet：create（管理员/团队组长/越权/层级保护/根节点约束）
  / update（层级保护）/ destroy（根节点/层级保护/有子节点/有文档/空节点软删）
"""
import json
import os
import tempfile
import uuid as uuid_lib
from unittest.mock import patch, MagicMock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.knowledge.models import (
    KnowledgeNode, Document, ResourceShare, ResourceBlockList,
    VisibilityLevel, ResourceType, ShareScopeType, AccessLevel, ShareStatus,
)
from apps.knowledge.views import (
    _normalize_visibility_level, _encode_ticket_reason, _decode_ticket_reason,
    _extract_last_comment, _detect_file_type, _build_tree, _get_user_role,
    DocumentUploadView,
)
from apps.knowledge.tests.test_views import (
    _get_or_create_role, _create_test_user, _auth_headers, _create_document,
    KnowledgeViewsExtraBase,
)
from apps.users.models import (
    User, Role, UserRoleRel, GrantStatus, Department, Team, Permission, RolePermissionRel,
    TicketList, TicketStatus, TicketChangeType,
)


class TestAllowedVisibility(KnowledgeViewsExtraBase):
    """AllowedVisibilityView 可选部门/团队测试"""

    @pytest.mark.integration
    def test_normal_user_visibility_options(self):
        """普通用户返回可选的部门/团队列表"""
        resp = self.client.get(
            '/api/v1/knowledge/documents/allowed_visibility/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data['role'] is None
        assert any(d['name'] == '研发部' for d in data['departments'])
        assert any(t['code'] == 'rd-backend' for t in data['teams'])

    @pytest.mark.integration
    def test_super_admin_role(self):
        """超管 role=super_admin"""
        resp = self.client.get(
            '/api/v1/knowledge/documents/allowed_visibility/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        assert resp.json()['role'] == 'super_admin'

    @pytest.mark.integration
    def test_result_is_cached(self):
        """结果写入缓存（cache key 含 role/dept/team_ids）"""
        from django.core.cache import cache
        self.client.get('/api/v1/knowledge/documents/allowed_visibility/',
                        **_auth_headers(self.normal_user))
        assert cache.get('allowed_visibility_None_None_()') is not None

class TestNodeTreeFilter(KnowledgeViewsExtraBase):
    """NodeTreeView root_type 过滤测试"""

    @pytest.mark.integration
    def test_filter_matching_root_type(self):
        """root_type=company_doc 返回全部节点"""
        resp = self.client.get(
            '/api/v1/knowledge/nodes/tree/?root_type=company_doc',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert resp.json()['total'] >= 4

    @pytest.mark.integration
    def test_filter_no_match(self):
        """root_type 无匹配 → 空树"""
        resp = self.client.get(
            '/api/v1/knowledge/nodes/tree/?root_type=hr_doc',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert resp.json()['tree'] == []


# ============================================================================
# KnowledgeNodeViewSet create / update / destroy
# ============================================================================

class TestNodeCreate(KnowledgeViewsExtraBase):
    """KnowledgeNodeViewSet.create 测试 —— 节点/文件夹分层 + 角色领地校验"""

    def _make_dept_manager(self, dept):
        """构造部门经理：user.manage 权限 + 主部门归属

        get_user_managed_depts 含主部门，故只要授予 user.manage 权限即为部门经理。
        """
        user = _create_test_user('mgr_' + dept.code)
        user.department = dept
        user.save(update_fields=['department'])
        role = _get_or_create_role('dept_manager')
        perm, _ = Permission.objects.get_or_create(
            permission_key='user.manage',
            defaults={'permission_name': '用户管理', 'module': 'user'})
        RolePermissionRel.objects.get_or_create(
            role=role, permission=perm,
            defaults={'granted_by': self.super_admin, 'is_active': True})
        UserRoleRel.objects.get_or_create(
            user=user, role=role,
            defaults={'status': GrantStatus.ACTIVE})
        return user

    @pytest.mark.integration
    def test_admin_create_category_under_team(self):
        """超管在团队节点下创建业务分类 → 201 且 path 正确、node_kind=FOLDER"""
        resp = self.client.post(
            '/api/v1/knowledge/nodes/',
            data=json.dumps({'parent': self.team_node.id, 'name': '新分类',
                             'node_type': 'folder'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 201
        # 创建序列化器不含 id 字段，通过 DB 验证落库结果
        node = KnowledgeNode.objects.get(name='新分类', node_type='folder',
                                         parent=self.team_node)
        assert node.path.startswith(self.team_node.path)
        assert node.node_level == 4
        assert node.node_kind == 'FOLDER'

    @pytest.mark.integration
    def test_admin_create_folder_under_root(self):
        """超管在 root 下创建文件夹 → 201（与部门节点同级，node_level=2）"""
        resp = self.client.post(
            '/api/v1/knowledge/nodes/',
            data=json.dumps({'parent': self.root_node.id, 'name': '公共资料',
                             'node_type': 'folder'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 201
        node = KnowledgeNode.objects.get(name='公共资料', parent=self.root_node)
        assert node.node_kind == 'FOLDER'
        assert node.node_level == 2

    def test_kb_admin_create_folder_under_root(self):
        """知识库管理员可在 root 下创建文件夹（与部门节点同级，node_level=2）"""
        kb_admin = _create_test_user('kb_admin_user')
        role = _get_or_create_role('kb_admin')
        perm, _ = Permission.objects.get_or_create(
            permission_key='kb.manage_all',
            defaults={'permission_name': '知识库管理', 'module': 'knowledge'})
        RolePermissionRel.objects.get_or_create(
            role=role, permission=perm,
            defaults={'granted_by': self.super_admin, 'is_active': True})
        UserRoleRel.objects.get_or_create(
            user=kb_admin, role=role,
            defaults={'status': GrantStatus.ACTIVE})
        resp = self.client.post(
            '/api/v1/knowledge/nodes/',
            data=json.dumps({'parent': self.root_node.id, 'name': '公共资料库',
                             'node_type': 'folder'}),
            content_type='application/json',
            **_auth_headers(kb_admin))
        assert resp.status_code == 201
        node = KnowledgeNode.objects.get(name='公共资料库', parent=self.root_node)
        assert node.node_kind == 'FOLDER'
        assert node.node_level == 2

    @pytest.mark.integration
    def test_admin_create_folder_under_dept_node(self):
        """超管在部门节点下创建文件夹 → 201（新语义：部门节点可挂文件夹）"""
        resp = self.client.post(
            '/api/v1/knowledge/nodes/',
            data=json.dumps({'parent': self.dept_node.id, 'name': '部门共享',
                             'node_type': 'folder'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 201
        node = KnowledgeNode.objects.get(name='部门共享', parent=self.dept_node)
        assert node.node_kind == 'FOLDER'
        assert node.node_level == 3

    @pytest.mark.integration
    def test_team_leader_create_in_own_team(self):
        """团队组长可在自己团队下创建分类节点"""
        resp = self.client.post(
            '/api/v1/knowledge/nodes/',
            data=json.dumps({'parent': self.team_node.id, 'name': '组长分类',
                             'node_type': 'folder'}),
            content_type='application/json',
            **_auth_headers(self.team_leader))
        assert resp.status_code == 201

    @pytest.mark.integration
    def test_team_leader_create_under_dept_node_denied(self):
        """团队组长在部门节点下创建 → 403（部门节点归部门经理/超管管理）"""
        resp = self.client.post(
            '/api/v1/knowledge/nodes/',
            data=json.dumps({'parent': self.dept_node.id, 'name': '越权',
                             'node_type': 'folder'}),
            content_type='application/json',
            **_auth_headers(self.team_leader))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_team_leader_create_outside_team_denied(self):
        """团队组长在非本团队节点下创建 → 403"""
        other_dept = Department.objects.create(name='市场部', code='mkt')
        other_node = self._create_node(
            '市场部', 'folder', node_level=2, parent=self.root_node,
            ref_id=other_dept.id)
        resp = self.client.post(
            '/api/v1/knowledge/nodes/',
            data=json.dumps({'parent': other_node.id, 'name': '越权分类',
                             'node_type': 'folder'}),
            content_type='application/json',
            **_auth_headers(self.team_leader))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_dept_manager_create_in_own_dept(self):
        """部门经理在本部门节点下创建文件夹 → 201"""
        mgr = self._make_dept_manager(self.dept)
        resp = self.client.post(
            '/api/v1/knowledge/nodes/',
            data=json.dumps({'parent': self.dept_node.id, 'name': '经理文件夹',
                             'node_type': 'folder'}),
            content_type='application/json',
            **_auth_headers(mgr))
        assert resp.status_code == 201

    @pytest.mark.integration
    def test_dept_manager_create_other_dept_denied(self):
        """部门经理在别的部门节点下创建 → 403"""
        mgr = self._make_dept_manager(self.dept)
        other_dept = Department.objects.create(name='市场部', code='mkt')
        other_node = self._create_node(
            '市场部', 'folder', node_level=2, parent=self.root_node,
            ref_id=other_dept.id)
        resp = self.client.post(
            '/api/v1/knowledge/nodes/',
            data=json.dumps({'parent': other_node.id, 'name': '越权',
                             'node_type': 'folder'}),
            content_type='application/json',
            **_auth_headers(mgr))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_normal_user_create_denied(self):
        """普通用户（非组长非管理员）创建 → 403"""
        resp = self.client.post(
            '/api/v1/knowledge/nodes/',
            data=json.dumps({'parent': self.team_node.id, 'name': 'x',
                             'node_type': 'folder'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_create_without_parent_denied(self):
        """文件夹必须指定上级节点 → 400"""
        resp = self.client.post(
            '/api/v1/knowledge/nodes/',
            data=json.dumps({'name': 'x', 'node_type': 'folder'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400
        assert '必须指定上级节点' in resp.json()['details']['parent']

    @pytest.mark.integration
    def test_create_root_manual_denied(self):
        """手动创建 root 节点（含指定/未指定父节点）→ 400，根节点由系统自动创建"""
        resp = self.client.post(
            '/api/v1/knowledge/nodes/',
            data=json.dumps({'parent': self.team_node.id, 'name': 'x',
                             'node_type': 'root'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400
        assert '根节点由系统自动创建' in resp.json()['details']['parent']
        # 未指定父节点的 root 创建同样被拦截
        resp = self.client.post(
            '/api/v1/knowledge/nodes/',
            data=json.dumps({'name': '新库', 'node_type': 'root',
                             'root_type': 'company_doc'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400

class TestNodeUpdate(KnowledgeViewsExtraBase):
    """KnowledgeNodeViewSet.update 测试"""

    @pytest.mark.integration
    def test_admin_update_category_200(self):
        """超管修改业务分类节点名称 → 200"""
        resp = self.client.patch(
            f'/api/v1/knowledge/nodes/{self.category_node.id}/',
            data=json.dumps({'name': '改名分类'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        self.category_node.refresh_from_db()
        assert self.category_node.name == '改名分类'

    @pytest.mark.integration
    def test_update_protected_level_denied(self):
        """修改 Level 3 团队节点 → 400 层级保护"""
        resp = self.client.patch(
            f'/api/v1/knowledge/nodes/{self.team_node.id}/',
            data=json.dumps({'name': 'x'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400
        # 自定义异常处理器将字段错误放在 details 内
        assert '不支持直接操作' in resp.json()['details']['detail']

    @pytest.mark.integration
    def test_update_visibility_creates_ticket(self):
        """修改文件夹可见范围 → 403 且自动创建双层审批工单，节点值不变"""
        resp = self.client.patch(
            f'/api/v1/knowledge/nodes/{self.category_node.id}/',
            data=json.dumps({'visibility_level': 'PUBLIC'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 403
        assert '工单' in resp.json()['details']['detail']
        ticket = TicketList.objects.filter(
            permission_detail__reason__startswith=f'[node:{self.category_node.id}:visibility_change]',
            status=TicketStatus.PENDING,
        ).first()
        assert ticket is not None
        assert len(ticket.approval_chain) == 2
        assert '目标值:PUBLIC' in ticket.reason
        # 节点可见范围未直接变更（待审批后写回）
        self.category_node.refresh_from_db()
        assert self.category_node.visibility_level is None

    @pytest.mark.integration
    def test_update_visibility_same_value_no_ticket(self):
        """可见范围未变化时正常保存，不创建工单"""
        resp = self.client.patch(
            f'/api/v1/knowledge/nodes/{self.category_node.id}/',
            data=json.dumps({'name': '改名'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        assert TicketList.objects.filter(
            permission_detail__reason__startswith=f'[node:{self.category_node.id}:visibility_change]',
        ).count() == 0

    def _make_dept_manager(self):
        """构造部门经理（user.manage + 主部门归属，get_user_managed_depts 含主部门）"""
        mgr = _create_test_user('mgr_rd')
        mgr.department = self.dept
        mgr.save(update_fields=['department'])
        role = _get_or_create_role('dept_manager')
        perm, _ = Permission.objects.get_or_create(
            permission_key='user.manage',
            defaults={'permission_name': '用户管理', 'module': 'user'})
        RolePermissionRel.objects.get_or_create(
            role=role, permission=perm,
            defaults={'granted_by': self.super_admin, 'is_active': True})
        UserRoleRel.objects.get_or_create(
            user=mgr, role=role,
            defaults={'status': GrantStatus.ACTIVE})
        return mgr

    @pytest.mark.integration
    def test_team_leader_visibility_chain_has_dept_leader(self):
        """团队级：组长发起可见范围变更 → 审批链第一节点为部门经理（DEPT_LEADER + 部门 scope）"""
        resp = self.client.patch(
            f'/api/v1/knowledge/nodes/{self.category_node.id}/',
            data=json.dumps({'visibility_level': 'PUBLIC'}),
            content_type='application/json',
            **_auth_headers(self.team_leader))
        assert resp.status_code == 403
        ticket = TicketList.objects.filter(
            permission_detail__reason__startswith=f'[node:{self.category_node.id}:visibility_change]',
            status=TicketStatus.PENDING,
        ).first()
        assert ticket is not None
        assert len(ticket.approval_chain) == 1
        step0 = ticket.approval_chain[0]
        assert step0['approver_role'] == 'DEPT_LEADER'
        assert step0['approver_scope_id'] == self.dept.id

    @pytest.mark.integration
    def test_dept_manager_visibility_chain_has_kb_admin(self):
        """部门级：部门经理发起可见范围变更 → 审批链第一节点为文档管理员/超管（KB_ADMIN）"""
        mgr = self._make_dept_manager()
        resp = self.client.patch(
            f'/api/v1/knowledge/nodes/{self.category_node.id}/',
            data=json.dumps({'visibility_level': 'PUBLIC'}),
            content_type='application/json',
            **_auth_headers(mgr))
        assert resp.status_code == 403
        ticket = TicketList.objects.filter(
            permission_detail__reason__startswith=f'[node:{self.category_node.id}:visibility_change]',
            status=TicketStatus.PENDING,
        ).first()
        assert ticket is not None
        assert len(ticket.approval_chain) == 1
        assert ticket.approval_chain[0]['approver_role'] == 'KB_ADMIN'

    @pytest.mark.integration
    def test_approve_visibility_ticket_by_dept_manager(self):
        """团队级工单由部门经理审批 → 通过后节点可见范围生效（单层审批直接写回）"""
        # 组长发起
        self.client.patch(
            f'/api/v1/knowledge/nodes/{self.category_node.id}/',
            data=json.dumps({'visibility_level': 'PUBLIC'}),
            content_type='application/json',
            **_auth_headers(self.team_leader))
        ticket = TicketList.objects.filter(
            permission_detail__reason__startswith=f'[node:{self.category_node.id}:visibility_change]',
            status=TicketStatus.PENDING,
        ).first()
        # 部门经理审批（匹配 DEPT_LEADER scope）
        mgr = self._make_dept_manager()
        resp = self.client.post(
            '/api/v1/knowledge/documents/approve_access_request/',
            data=json.dumps({'request_id': ticket.id, 'comment': '同意'}),
            content_type='application/json',
            **_auth_headers(mgr))
        assert resp.status_code == 200, resp.content
        self.category_node.refresh_from_db()
        assert self.category_node.visibility_level == VisibilityLevel.PUBLIC

    @pytest.mark.integration
    def test_approve_visibility_ticket_wrong_role_denied(self):
        """团队级工单非部门经理审批 → 403（审批链按角色指派校验生效）"""
        self.client.patch(
            f'/api/v1/knowledge/nodes/{self.category_node.id}/',
            data=json.dumps({'visibility_level': 'PUBLIC'}),
            content_type='application/json',
            **_auth_headers(self.team_leader))
        ticket = TicketList.objects.filter(
            permission_detail__reason__startswith=f'[node:{self.category_node.id}:visibility_change]',
            status=TicketStatus.PENDING,
        ).first()
        # 普通用户（无 user.manage、非管理员）审批 → 403
        resp = self.client.post(
            '/api/v1/knowledge/documents/approve_access_request/',
            data=json.dumps({'request_id': ticket.id, 'comment': '越权'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.PENDING

    @pytest.mark.integration
    def test_approve_visibility_ticket_super_admin_chain_double(self):
        """超管发起 → 双管理员复核链，需两位管理员先后审批"""
        self.client.patch(
            f'/api/v1/knowledge/nodes/{self.category_node.id}/',
            data=json.dumps({'visibility_level': 'PUBLIC'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        ticket = TicketList.objects.filter(
            permission_detail__reason__startswith=f'[node:{self.category_node.id}:visibility_change]',
            status=TicketStatus.PENDING,
        ).first()
        assert len(ticket.approval_chain) == 2
        assert ticket.approval_chain[0]['approver_role'] == 'KB_ADMIN'
        # 第一审通过 → 仍 pending
        resp = self.client.post(
            '/api/v1/knowledge/documents/approve_access_request/',
            data=json.dumps({'request_id': ticket.id, 'comment': '一审同意'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200, resp.content
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.PENDING
        # 第二审由另一位超管通过 → 生效
        resp = self.client.post(
            '/api/v1/knowledge/documents/approve_access_request/',
            data=json.dumps({'request_id': ticket.id, 'comment': '复核同意'}),
            content_type='application/json',
            **_auth_headers(self.admin2))
        assert resp.status_code == 200, resp.content
        self.category_node.refresh_from_db()
        assert self.category_node.visibility_level == VisibilityLevel.PUBLIC

class TestNodeDestroy(KnowledgeViewsExtraBase):
    """KnowledgeNodeViewSet.destroy 测试"""

    @pytest.mark.integration
    def test_destroy_root_denied(self):
        """删除根节点 → 400（Level 1 保护分支优先于根节点分支）"""
        resp = self.client.delete(
            f'/api/v1/knowledge/nodes/{self.root_node.id}/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400
        # 源码顺序：node_level<=3 的 Level 保护先命中，返回“不支持直接删除”
        assert '不支持直接删除' in resp.json()['detail']

    @pytest.mark.integration
    def test_destroy_protected_level_denied(self):
        """删除 Level 3 团队节点 → 400"""
        resp = self.client.delete(
            f'/api/v1/knowledge/nodes/{self.team_node.id}/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_destroy_node_with_children_denied(self):
        """删除含子节点的分类 → 400（需先删子分类）"""
        child = self._create_node(
            '子分类', 'folder', node_level=5, parent=self.category_node)
        resp = self.client.delete(
            f'/api/v1/knowledge/nodes/{self.category_node.id}/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400
        assert '子分类' in resp.json()['detail']

    @pytest.mark.integration
    def test_destroy_node_with_docs_denied(self):
        """删除含文档的分类 → 400（需先迁移文档）"""
        resp = self.client.delete(
            f'/api/v1/knowledge/nodes/{self.category_node.id}/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400
        assert '个文档' in resp.json()['detail']

    @pytest.mark.integration
    def test_destroy_empty_node_soft_delete_204(self):
        """删除空分类节点 → 204 软删（is_deleted=True）"""
        empty = self._create_node(
            '空分类', 'folder', node_level=5, parent=self.team_node)
        resp = self.client.delete(
            f'/api/v1/knowledge/nodes/{empty.id}/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 204
        empty.refresh_from_db()
        assert empty.is_deleted is True

    @pytest.mark.integration
    def test_team_leader_destroy_own_team_node(self):
        """团队组长删除本团队下的空分类节点 → 204"""
        empty = self._create_node(
            '组长分类', 'folder', node_level=5, parent=self.team_node)
        resp = self.client.delete(
            f'/api/v1/knowledge/nodes/{empty.id}/',
            **_auth_headers(self.team_leader))
        assert resp.status_code == 204


# ============================================================================
# DocumentViewSet restore / hard_delete / reparse / download / raw_content
# ============================================================================

