"""
apps.knowledge.views 文档双审补充测试 —— 待审核列表 / 审核通过 / 审核驳回

覆盖范围：
- PendingAuditsView：团队组长视角的待审核文档列表
- DocumentAuditApproveView：双审通过（含超管复核绕过）
- DocumentAuditRejectView：审核驳回
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
    TicketList, TicketStatus, TicketChangeType,
)


class TestDocAuditPending(KnowledgeViewsExtraBase):
    """DocAuditPendingView 待审核列表测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """在基类环境基础上补充待审文档"""
        self._init_env()
        # Document.audit_status 默认值为 pending_team，先把基类文档置为终态，
        # 避免它们混入待审列表
        Document.objects.filter(id__in=[
            self.doc_own_private.id, self.doc_other_public.id,
            self.doc_other_private.id,
        ]).update(audit_status='passed')
        # 待团队组长审核的文档（挂在本团队下）
        self.pending_doc = _create_document(
            self.category_node, self.normal_user, team_id=self.team.id,
            dept_id=self.dept.id, title='待审文档', file_name='audit.txt',
            audit_status='pending_team')

    @pytest.mark.integration
    def test_team_leader_sees_own_team_docs(self):
        """团队组长看到本团队 pending_team 文档，audit_step 标注组长审核"""
        resp = self.client.get(
            '/api/v1/knowledge/documents/pending-audits/',
            **_auth_headers(self.team_leader))
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert len(rows) == 1
        assert rows[0]['id'] == self.pending_doc.id
        assert '团队组长' in rows[0]['audit_step']

    @pytest.mark.integration
    def test_super_admin_sees_all(self):
        """超管看到全部待审文档（管理员代审）"""
        resp = self.client.get(
            '/api/v1/knowledge/documents/pending-audits/',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        ids = {r['id'] for r in resp.json()['rows']}
        assert self.pending_doc.id in ids

    @pytest.mark.integration
    def test_normal_user_denied_403(self):
        """无审核权限的普通用户 → 403"""
        resp = self.client.get(
            '/api/v1/knowledge/documents/pending-audits/',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

class TestDocAuditApprove(KnowledgeViewsExtraBase):
    """DocAuditApproveView 审核通过测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """在基类环境基础上补充待审文档"""
        self._init_env()
        self.pending_doc = _create_document(
            self.category_node, self.normal_user, team_id=self.team.id,
            dept_id=self.dept.id, title='待审文档', file_name='audit.txt',
            audit_status='pending_team')

    @pytest.mark.integration
    def test_approve_pending_team_to_compliance(self):
        """审核通过（pending_team→pending_compliance）"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.pending_doc.id}/audit-approve/',
            data=json.dumps({'comment': '内容合规'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        assert resp.json()['audit_status'] == 'pending_compliance'

    @pytest.mark.integration
    def test_approve_compliance_to_passed(self):
        """复核通过（pending_compliance→passed）"""
        self.pending_doc.audit_status = 'pending_compliance'
        self.pending_doc.save(update_fields=['audit_status'])
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.pending_doc.id}/audit-approve/',
            data=json.dumps({'comment': '复核通过'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        assert resp.json()['audit_status'] == 'passed'

    @pytest.mark.integration
    def test_approve_not_authorized_403(self):
        """无审核权限 → 403"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.pending_doc.id}/audit-approve/',
            data=json.dumps({'comment': 'x'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_approve_final_state_400(self):
        """终态（passed）不可再审核 → 400"""
        self.pending_doc.audit_status = 'passed'
        self.pending_doc.save(update_fields=['audit_status'])
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.pending_doc.id}/audit-approve/',
            data=json.dumps({'comment': 'x'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_approve_missing_doc_404(self):
        """文档不存在 → 404"""
        resp = self.client.post(
            '/api/v1/knowledge/documents/999999/audit-approve/',
            data=json.dumps({'comment': 'x'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 404

class TestDocAuditReject(KnowledgeViewsExtraBase):
    """DocAuditRejectView 审核驳回测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """在基类环境基础上补充待审文档"""
        self._init_env()
        self.pending_doc = _create_document(
            self.category_node, self.normal_user, team_id=self.team.id,
            dept_id=self.dept.id, title='待审文档', file_name='audit.txt',
            audit_status='pending_team')

    @pytest.mark.integration
    def test_reject_requires_comment(self):
        """驳回理由必填 → 400"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.pending_doc.id}/audit-reject/',
            data=json.dumps({'comment': '  '}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400
        assert '驳回理由不能为空' in resp.json()['detail']

    @pytest.mark.integration
    def test_reject_sets_rejected(self):
        """驳回 → audit_status=rejected"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.pending_doc.id}/audit-reject/',
            data=json.dumps({'comment': '资料不全'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 200
        assert resp.json()['audit_status'] == 'rejected'
        assert resp.json()['reject_comment'] == '资料不全'

    @pytest.mark.integration
    def test_reject_not_authorized_403(self):
        """无审核权限 → 403"""
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.pending_doc.id}/audit-reject/',
            data=json.dumps({'comment': 'x'}),
            content_type='application/json',
            **_auth_headers(self.normal_user))
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_reject_final_state_400(self):
        """终态（rejected）不可再驳回 → 400"""
        self.pending_doc.audit_status = 'rejected'
        self.pending_doc.save(update_fields=['audit_status'])
        resp = self.client.post(
            f'/api/v1/knowledge/documents/{self.pending_doc.id}/audit-reject/',
            data=json.dumps({'comment': 'x'}),
            content_type='application/json',
            **_auth_headers(self.super_admin))
        assert resp.status_code == 400


# ============================================================================
# CeleryStatusView（broker 不可用降级）
# ============================================================================

