"""
apps.knowledge.views 访问授权补充测试 —— 申请 / 授权 / 审批 / 驳回全链路

与 test_views.py 互补：
- AvailableDeptsView：可选部门列表
- request_access / my_access_requests / pending_access_requests
- grant_access / access_grants / revoke_grant
- approve_access_request（单层+双层）/ reject_access_request
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


class TestAvailableDepts(KnowledgeViewsExtraBase):
    """DocumentViewSet.available_depts 部门列表测试"""

    @pytest.mark.integration
    def test_available_depts_200(self):
        """已登录用户可获取活跃部门列表"""
        resp = self.client.get(
            '/api/v1/knowledge/documents/available_depts/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert any(d['name'] == '研发部' for d in resp.json())


# ============================================================================
# 文档访问授权全链路
# ============================================================================

class TestRequestAccess(KnowledgeViewsExtraBase):
    """request_access 申请测试"""

    @pytest.mark.integration
    def test_request_access_201(self):
        """他人申请读取私有文档 → 201 创建审批工单"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/request_access/',
            data=json.dumps({'action': 'read', 'reason': '项目需要'}),
            content_type='application/json',
            **_auth_headers(self.other_user))
        assert resp.status_code == 201
        data = resp.json()
        assert data['action'] == 'read'
        assert data['status'] == TicketStatus.PENDING

    @pytest.mark.integration
    def test_request_access_duplicate_returns_ok_false(self):
        """相同 pending 申请不重复创建 → 200 ok=false"""
        self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/request_access/',
            data=json.dumps({'action': 'read', 'reason': '项目需要'}),
            content_type='application/json',
            **_auth_headers(self.other_user))
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/request_access/',
            data=json.dumps({'action': 'read', 'reason': '项目需要'}),
            content_type='application/json',
            **_auth_headers(self.other_user))
        assert resp.status_code == 200
        assert resp.json()['ok'] is False
        assert '已存在' in resp.json()['detail']

    @pytest.mark.integration
    def test_request_access_invalid_action_400(self):
        """非法申请类型 → 400"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/request_access/',
            data=json.dumps({'action': 'delete'}),
            content_type='application/json',
            **_auth_headers(self.other_user))
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_request_access_doc_not_found_404(self):
        """文档不存在 → 404"""
        resp = self.client.post(
            '/api/v1/knowledge/documents/999999/request_access/',
            data=json.dumps({'action': 'read'}),
            content_type='application/json',
            **_auth_headers(self.other_user))
        assert resp.status_code == 404

class TestMyAndPendingAccessRequests(KnowledgeViewsExtraBase):
    """my_access_requests / pending_access_requests 测试"""

    @pytest.mark.integration
    def test_my_access_requests(self):
        """我发起的申请列表（含 reason 解码）"""
        self._request_ticket(self.other_user, self.doc_own_private, 'read')
        self._request_ticket(self.other_user, self.doc_own_private, 'download')
        resp = self.client.get(
            '/api/v1/knowledge/documents/my_access_requests/',
            **_auth_headers(self.other_user))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert all(d['target_type'] == 'doc' for d in data)
        assert all(d['target_id'] == self.doc_own_private.id for d in data)

    @pytest.mark.integration
    def test_pending_admin_sees_all(self):
        """管理员看到全部待审批申请"""
        self._request_ticket(self.other_user, self.doc_own_private, 'read')
        resp = self.client.get(
            '/api/v1/knowledge/documents/pending_access_requests/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    @pytest.mark.integration
    def test_pending_non_admin_only_own_docs(self):
        """非管理员只看到自己文档的申请（reason 前缀匹配）"""
        self._request_ticket(self.other_user, self.doc_own_private, 'read')  # normal_user 的文档
        self._request_ticket(self.other_user, self.doc_other_public, 'read')  # 超管的文档
        resp = self.client.get(
            '/api/v1/knowledge/documents/pending_access_requests/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]['target_id'] == self.doc_own_private.id

class TestGrantAndRevoke(KnowledgeViewsExtraBase):
    """grant_access / access_grants / revoke_grant 测试"""

    @pytest.mark.integration
    def test_grant_cross_team(self):
        """按 team_code 跨团队共享 → ResourceShare 创建 + has_resource_share 置位"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/grant_access/',
            data=json.dumps({'grant_type': 'cross_team', 'team_code': 'rd-backend'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert resp.json()['created'] is True
        share = ResourceShare.objects.get(
            resource_type=ResourceType.DOCUMENT, resource_id=self.doc_own_private.id,
            share_scope_type=ShareScopeType.TEAM)
        assert share.share_scope_id == self.team.id
        self.doc_own_private.refresh_from_db()
        assert self.doc_own_private.has_resource_share is True

    @pytest.mark.integration
    def test_grant_cross_team_repeat_is_idempotent(self):
        """重复授予同一团队 → created=False 不重复建记录"""
        self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/grant_access/',
            data=json.dumps({'grant_type': 'cross_team', 'team_code': 'rd-backend'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/grant_access/',
            data=json.dumps({'grant_type': 'cross_team', 'team_code': 'rd-backend'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.json()['created'] is False
        assert ResourceShare.objects.filter(
            resource_type=ResourceType.DOCUMENT, resource_id=self.doc_own_private.id,
            share_scope_type=ShareScopeType.TEAM).count() == 1

    @pytest.mark.integration
    def test_grant_cross_team_errors(self):
        """team_code 缺失 / 团队不存在 → 400"""
        headers = _auth_headers(self.normal_user)
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/grant_access/',
            data=json.dumps({'grant_type': 'cross_team'}),
            content_type='application/json', **headers)
        assert resp.status_code == 400
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/grant_access/',
            data=json.dumps({'grant_type': 'cross_team', 'team_code': 'no-such-team'}),
            content_type='application/json', **headers)
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_grant_allow_user(self):
        """按 uid 个人共享 → ResourceShare USER 记录"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/grant_access/',
            data=json.dumps({'grant_type': 'allow_user', 'uid': self.other_user.id}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert ResourceShare.objects.filter(
            resource_type=ResourceType.DOCUMENT, resource_id=self.doc_own_private.id,
            share_scope_type=ShareScopeType.USER,
            share_scope_id=self.other_user.id).exists()

    @pytest.mark.integration
    def test_grant_invalid_type_400(self):
        """非法 grant_type → 400"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/grant_access/',
            data=json.dumps({'grant_type': 'deny_user'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_grant_non_owner_403(self):
        """非 Owner 授权 → 403"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_other_public.id}/grant_access/',
            data=json.dumps({'grant_type': 'allow_user', 'uid': self.normal_user.id}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_access_grants_lists_all_kinds(self):
        """access_grants 汇总白名单/黑名单"""
        ResourceShare.objects.create(
            resource_type=ResourceType.DOCUMENT, resource_id=self.doc_own_private.id,
            share_scope_type=ShareScopeType.USER, share_scope_id=self.other_user.id,
            access_level=AccessLevel.READ, granted_by=self.normal_user,
            status=ShareStatus.ACTIVE)
        ResourceShare.objects.create(
            resource_type=ResourceType.DOCUMENT, resource_id=self.doc_own_private.id,
            share_scope_type=ShareScopeType.TEAM, share_scope_id=self.team.id,
            access_level=AccessLevel.READ, granted_by=self.normal_user,
            status=ShareStatus.ACTIVE)
        ResourceBlockList.objects.create(
            resource_type=ResourceType.DOCUMENT, resource_id=self.doc_own_private.id,
            blocked_user=self.other_user, reason='涉密剔除', blocked_by=self.normal_user,
            status=ShareStatus.ACTIVE)
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/access_grants/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data['allow_users']) == 1
        assert len(data['cross_teams']) == 1
        assert len(data['deny_users']) == 1
        assert data['deny_users'][0]['reason'] == '涉密剔除'

    @pytest.mark.integration
    def test_access_grants_non_owner_403(self):
        """非 Owner 查看授权列表 → 403"""
        resp = self.client.get(
            f'/api/v1/knowledge/documents/{self.doc_other_public.id}/access_grants/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_revoke_share(self):
        """撤销白名单授权 → status=REVOKED 软撤销保留审计"""
        share = ResourceShare.objects.create(
            resource_type=ResourceType.DOCUMENT, resource_id=self.doc_own_private.id,
            share_scope_type=ShareScopeType.USER, share_scope_id=self.other_user.id,
            access_level=AccessLevel.READ, granted_by=self.normal_user,
            status=ShareStatus.ACTIVE)
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/revoke_grant/',
            data=json.dumps({'grant_type': 'allow_user', 'grant_id': share.id}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        share.refresh_from_db()
        assert share.status == ShareStatus.REVOKED
        assert share.revoked_by_id == self.normal_user.id

    @pytest.mark.integration
    def test_revoke_block(self):
        """撤销黑名单 → ResourceBlockList.status=REVOKED"""
        block = ResourceBlockList.objects.create(
            resource_type=ResourceType.DOCUMENT, resource_id=self.doc_own_private.id,
            blocked_user=self.other_user, reason='临时封禁', blocked_by=self.normal_user,
            status=ShareStatus.ACTIVE)
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/revoke_grant/',
            data=json.dumps({'grant_type': 'deny_user', 'grant_id': block.id}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        block.refresh_from_db()
        assert block.status == ShareStatus.REVOKED

    @pytest.mark.integration
    def test_revoke_missing_404(self):
        """撤销不存在的授权 → 404"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/revoke_grant/',
            data=json.dumps({'grant_type': 'allow_user', 'grant_id': 999999}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 404

class TestApproveAccessRequest(KnowledgeViewsExtraBase):
    """approve_access_request 单层/双层审批测试"""

    @pytest.mark.integration
    def test_approve_single_step_grant(self):
        """单层审批：批准读取申请 → 创建 ResourceShare + 工单 EXECUTED"""
        ticket = self._request_ticket(self.other_user, self.doc_own_private, 'read')
        resp = self.client.post(
            '/api/v1/knowledge/documents/approve_access_request/',
            data=json.dumps({'request_id': ticket.id, 'comment': '同意'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert resp.json()['status'] == TicketStatus.EXECUTED
        share = ResourceShare.objects.get(
            resource_type=ResourceType.DOCUMENT, resource_id=self.doc_own_private.id,
            share_scope_type=ShareScopeType.USER, share_scope_id=self.other_user.id)
        assert share.status == ShareStatus.ACTIVE
        self.doc_own_private.refresh_from_db()
        assert self.doc_own_private.has_resource_share is True

    @pytest.mark.integration
    def test_approve_non_owner_403(self):
        """非 Owner/管理员审批 → 403"""
        ticket = self._request_ticket(self.other_user, self.doc_own_private, 'read')
        resp = self.client.post(
            '/api/v1/knowledge/documents/approve_access_request/',
            data=json.dumps({'request_id': ticket.id}),
            content_type='application/json',
            **_auth_headers(self.other_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_approve_missing_ticket_404(self):
        """工单不存在或已处理 → 404"""
        resp = self.client.post(
            '/api/v1/knowledge/documents/approve_access_request/',
            data=json.dumps({'request_id': 999999}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 404

    def _create_visibility_ticket(self):
        """Owner 扩大可见性：PATCH PUBLIC 触发 403 + 双层审批工单"""
        resp = self.client.patch(
            f'/api/v1/knowledge/documents/{self.doc_own_private.id}/',
            data=json.dumps({'visibility_level': 'PUBLIC'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403
        ticket = PermissionApprovalTicket.objects.get(
            change_type=TicketChangeType.SCOPE_CHANGE)
        return ticket

    @pytest.mark.integration
    def test_visibility_expand_creates_double_step_ticket(self):
        """可见性扩大（TEAM_ONLY→PUBLIC）→ 403 + 生成双层审批工单"""
        ticket = self._create_visibility_ticket()
        assert len(ticket.approval_chain) == 2
        assert ticket.status == TicketStatus.PENDING

    @pytest.mark.integration
    def test_visibility_double_step_approval(self):
        """双层审批：两位不同管理员先后审批 → EXECUTED + 可见性改为 PUBLIC"""
        ticket = self._create_visibility_ticket()
        # 第一步：admin1 审批 → 保持 PENDING 等待复核
        resp = self.client.post(
            '/api/v1/knowledge/documents/approve_access_request/',
            data=json.dumps({'request_id': ticket.id, 'comment': '审核通过'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        assert resp.json()['status'] == TicketStatus.PENDING
        ticket.refresh_from_db()
        assert ticket.current_step == 1
        # 第二步：admin2 复核 → EXECUTED 并写回可见性
        resp = self.client.post(
            '/api/v1/knowledge/documents/approve_access_request/',
            data=json.dumps({'request_id': ticket.id, 'comment': '复核通过'}),
            content_type='application/json',
            **_auth_headers(self.admin2))
        assert resp.status_code == 200
        assert resp.json()['status'] == TicketStatus.EXECUTED
        self.doc_own_private.refresh_from_db()
        # 源码行为：reason 中未编码可见性代码，兜底写 PUBLIC
        assert self.doc_own_private.visibility_level == VisibilityLevel.PUBLIC

    @pytest.mark.integration
    def test_visibility_same_admin_double_denied(self):
        """双层审批不能由同一管理员完成 → 403"""
        ticket = self._create_visibility_ticket()
        self.client.post(
            '/api/v1/knowledge/documents/approve_access_request/',
            data=json.dumps({'request_id': ticket.id}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        resp = self.client.post(
            '/api/v1/knowledge/documents/approve_access_request/',
            data=json.dumps({'request_id': ticket.id}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 403
        # 同一管理员拒绝是 PermissionDenied → 自定义异常处理器放在 details.detail
        assert '不能由同一管理员' in resp.json()['details']['detail']

class TestRejectAccessRequest(KnowledgeViewsExtraBase):
    """reject_access_request 驳回测试"""

    @pytest.mark.integration
    def test_reject_200(self):
        """Owner 驳回 → REJECTED + 驳回意见记录进 approval_chain"""
        ticket = self._request_ticket(self.other_user, self.doc_own_private, 'read')
        resp = self.client.post(
            '/api/v1/knowledge/documents/reject_access_request/',
            data=json.dumps({'request_id': ticket.id, 'comment': '不符合条件'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 200
        assert resp.json()['status'] == TicketStatus.REJECTED
        ticket.refresh_from_db()
        assert ticket.approval_chain[0]['comment'] == '不符合条件'
        # 驳回不创建任何共享授权
        assert not ResourceShare.objects.filter(resource_id=self.doc_own_private.id).exists()

    @pytest.mark.integration
    def test_reject_non_owner_403(self):
        """非 Owner 驳回 → 403"""
        ticket = self._request_ticket(self.other_user, self.doc_own_private, 'read')
        resp = self.client.post(
            '/api/v1/knowledge/documents/reject_access_request/',
            data=json.dumps({'request_id': ticket.id}),
            content_type='application/json',
            **_auth_headers(self.other_user))
        assert resp.status_code == 403


# ============================================================================
# DocumentUploadView 补充分支
# ============================================================================

