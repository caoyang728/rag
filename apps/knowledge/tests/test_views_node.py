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
    User, Role, UserRoleRel, GrantStatus, Department, Team,
    PermissionApprovalTicket, TicketStatus, TicketChangeType,
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
    """KnowledgeNodeViewSet.create 测试"""

    @pytest.mark.integration
    def test_admin_create_category_under_team(self):
        """超管在团队节点下创建业务分类 → 201 且 path 正确"""
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
    def test_create_protected_level_denied(self):
        """在部门节点（Level 2 保护层）下创建 → 400 层级保护"""
        resp = self.client.post(
            '/api/v1/knowledge/nodes/',
            data=json.dumps({'parent': self.dept_node.id, 'name': 'x',
                             'node_type': 'folder'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400
        # 自定义异常处理器将字段错误放在 details 内
        assert '不支持直接创建' in resp.json()['details']['parent']

    @pytest.mark.integration
    def test_create_root_with_parent_denied(self):
        """root 节点指定父节点 → 400"""
        resp = self.client.post(
            '/api/v1/knowledge/nodes/',
            data=json.dumps({'parent': self.team_node.id, 'name': 'x',
                             'node_type': 'root'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400
        assert '根节点不能指定上级节点' in resp.json()['details']['parent']

    @pytest.mark.integration
    def test_create_root_without_parent_blocked_by_level_guard(self):
        """无父节点的 root 创建同样被 Level 保护拦截（depth=0）—— 固化源码行为"""
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

