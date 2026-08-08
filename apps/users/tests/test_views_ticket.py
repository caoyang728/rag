"""
apps.users.views 权限工单补充测试 —— 申请 / 撤回 / 审批 / 驳回 / 查询全链路

与 test_views.py 互补：AccessApplicationView / withdraw / approve / reject /
assignable-roles / approval-chain-preview。
（旧列表接口 pending-approvals / processed-tickets / my-tickets / all-tickets
已下线，统一收敛到工单中心 /api/v1/auth/tickets/，见 test_views_ticket_center.py）
"""
import csv
import io
import json
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken

from apps.users.models import (
    User, Role, Department, Team, Permission, RolePermissionRel,
    UserRoleRel, UserDeptScopeRel, UserTeamScopeRel,
    TicketList, TicketStatus, TicketChangeType, ScopeType,
    GrantStatus, RoleType, DataScope,
)
from apps.users.tests.test_views_base import (
    _get_or_create_role, _create_user, _grant_permission, _grant_global_role,
    _auth_headers, FakeRedis, UsersAPIExtraBase,
)
from apps.users.ticket_service import create_ticket, approve_ticket


class TestAccessApplicationView(UsersAPIExtraBase):
    """权限申请接口：字段校验 + 业务规则（超管不可自助申请 / ROLE_CHANGE 前置校验）"""

    @pytest.mark.integration
    def test_get_applications_empty_200(self):
        """无工单时返回空列表 → 200"""
        resp = self.client.get('/api/v1/auth/permissions/applications/', **self.normal_headers)
        assert resp.status_code == 200
        assert resp.json()['count'] == 0

    @pytest.mark.integration
    def test_get_applications_only_own_tickets(self):
        """GET 只返回当前用户发起的工单（含 scope_name 与审批链序列化）"""
        self._create_pending_ticket()  # normal_user 发起（跨部门 viewer → 市场一组）
        self._create_pending_ticket(applicant=self.team_leader)  # 他人工单不应出现
        resp = self.client.get('/api/v1/auth/permissions/applications/', **self.normal_headers)
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert len(rows) == 1
        assert rows[0]['scope_name'] == '市场一组'
        assert 'approval_chain' in rows[0]

    @pytest.mark.integration
    def test_get_applications_approver_name_after_approval(self):
        """已审批工单的返回中带审批人姓名与审批意见（approver_name/reviewer_comment）"""
        _grant_global_role(self.super_admin, 'super_admin')
        t = self._create_pending_ticket()
        approve_ticket(t, self.super_admin, comment='同意')
        resp = self.client.get('/api/v1/auth/permissions/applications/', **self.normal_headers)
        row = resp.json()['rows'][0]
        assert row['approver_name'] == 'admin'
        assert row['reviewer_comment'] == '同意'

    @pytest.mark.integration
    def test_post_missing_role_key_400(self):
        """缺少 role_key → 400"""
        resp = self.client.post(
            '/api/v1/auth/permissions/applications/',
            data=json.dumps({'scope_type': 'TEAM', 'scope_id': self.team_b.id, 'reason': 'r'}),
            content_type='application/json', **self.normal_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_post_invalid_scope_type_400(self):
        """scope_type 取值非法 → 400"""
        resp = self.client.post(
            '/api/v1/auth/permissions/applications/',
            data=json.dumps({'role_key': 'viewer', 'scope_type': 'XXX', 'reason': 'r'}),
            content_type='application/json', **self.normal_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_post_invalid_change_type_400(self):
        """change_type 取值非法 → 400"""
        resp = self.client.post(
            '/api/v1/auth/permissions/applications/',
            data=json.dumps({'role_key': 'viewer', 'change_type': 'FOO', 'reason': 'r'}),
            content_type='application/json', **self.normal_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_post_missing_reason_400(self):
        """申请理由为空 → 400"""
        resp = self.client.post(
            '/api/v1/auth/permissions/applications/',
            data=json.dumps({'role_key': 'viewer', 'scope_type': 'TEAM',
                             'scope_id': self.team_b.id, 'reason': '  '}),
            content_type='application/json', **self.normal_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_post_scope_id_required_400(self):
        """scope_type=TEAM 时必须提供 scope_id → 400"""
        resp = self.client.post(
            '/api/v1/auth/permissions/applications/',
            data=json.dumps({'role_key': 'viewer', 'scope_type': 'TEAM', 'reason': 'r'}),
            content_type='application/json', **self.normal_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_post_role_not_found_400(self):
        """申请不存在的角色 → 400"""
        resp = self.client.post(
            '/api/v1/auth/permissions/applications/',
            data=json.dumps({'role_key': 'ghost_role', 'scope_type': 'NONE', 'reason': 'r'}),
            content_type='application/json', **self.normal_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_post_super_admin_self_apply_403(self):
        """super_admin 角色不可自助申请 → 403（只能由现有超管发起双人复核工单）"""
        resp = self.client.post(
            '/api/v1/auth/permissions/applications/',
            data=json.dumps({'role_key': 'super_admin', 'scope_type': 'NONE', 'reason': 'r'}),
            content_type='application/json', **self.normal_headers,
        )
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_post_role_change_missing_previous_role_400(self):
        """ROLE_CHANGE 必须提供 previous_role_id → 400"""
        # 协作角色由资源团队组长(team_a 组长)代被授权人提单,缺 previous_role_id 在校验层拦截
        resp = self.client.post(
            '/api/v1/auth/permissions/applications/',
            data=json.dumps({'role_key': 'viewer', 'scope_type': 'TEAM',
                             'scope_id': self.team_a.id, 'change_type': 'ROLE_CHANGE',
                             'reason': 'r', 'target_user_id': self.normal_user.id}),
            content_type='application/json', **self.leader_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_post_role_change_previous_role_not_found_400(self):
        """ROLE_CHANGE 的 previous_role_id 对应角色不存在 → 400"""
        resp = self.client.post(
            '/api/v1/auth/permissions/applications/',
            data=json.dumps({'role_key': 'viewer', 'scope_type': 'TEAM',
                             'scope_id': self.team_a.id, 'change_type': 'ROLE_CHANGE',
                             'previous_role_id': 999999, 'reason': 'r',
                             'target_user_id': self.normal_user.id}),
            content_type='application/json', **self.leader_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_post_create_ticket_201(self):
        """资源所有者(超管兜底)提交跨部门 viewer 申请 → 201 且工单 PENDING

        定稿后协作角色不再自助申请,统一由资源团队组长/部门经理代被授权人提单;
        team_c(市场一组)所属部门无部门经理,超管作为资源所有者兜底提单。
        """
        resp = self.client.post(
            '/api/v1/auth/permissions/applications/',
            data=json.dumps({'role_key': 'viewer', 'scope_type': 'TEAM',
                             'scope_id': self._team_c().id, 'reason': '需要查看市场一组资料',
                             'target_user_id': self.normal_user.id}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data['status'] == 'PENDING'
        ticket = TicketList.objects.get(id=data['id'])
        assert ticket.applicant_id == self.super_admin.id
        assert ticket.target_user_id == self.normal_user.id  # 资源所有者代被授权人提单

    @pytest.mark.integration
    def test_post_management_role_self_apply_403(self):
        """管理岗(user_admin 等)禁止自助申请 → 403(仅允许上级发起任命)"""
        _get_or_create_role('user_admin')
        resp = self.client.post(
            '/api/v1/auth/permissions/applications/',
            data=json.dumps({'role_key': 'user_admin', 'scope_type': 'NONE', 'reason': '申请用户管理员'}),
            content_type='application/json', **self.normal_headers,
        )
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_post_anonymous_401(self):
        """匿名提交申请 → 401"""
        resp = self.client.post('/api/v1/auth/permissions/applications/',
                                data=json.dumps({}), content_type='application/json')
        assert resp.status_code in (401, 403)


# ============================================================================
# AccessApplicationWithdrawView —— 撤回申请
# ============================================================================

class TestAccessApplicationWithdrawView(UsersAPIExtraBase):
    """撤回申请：仅申请人可撤回 PENDING 工单"""

    @pytest.mark.integration
    def test_withdraw_not_found_404(self):
        """撤回不存在的工单 → 404"""
        resp = self.client.post('/api/v1/auth/permissions/applications/999999/withdraw/',
                                **self.normal_headers)
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_withdraw_other_users_ticket_404(self):
        """撤回他人工单 → 404（查询限定 applicant=当前用户，看不到即不存在）"""
        t = self._create_pending_ticket(applicant=self.team_leader)
        resp = self.client.post(f'/api/v1/auth/permissions/applications/{t.id}/withdraw/',
                                **self.normal_headers)
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_withdraw_non_pending_400(self):
        """非 PENDING 状态的工单不可撤回 → 400"""
        t = self._create_pending_ticket()
        t.status = TicketStatus.EXECUTED
        t.save(update_fields=['status', 'updated_at'])
        resp = self.client.post(f'/api/v1/auth/permissions/applications/{t.id}/withdraw/',
                                **self.normal_headers)
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_withdraw_success(self):
        """PENDING 工单撤回 → 200 且状态流转为 CANCELLED"""
        t = self._create_pending_ticket()
        resp = self.client.post(f'/api/v1/auth/permissions/applications/{t.id}/withdraw/',
                                **self.normal_headers)
        assert resp.status_code == 200
        assert resp.json()['status'] == 'cancelled'
        t.refresh_from_db()
        assert t.status == TicketStatus.CANCELLED


# ============================================================================
# TicketApproveView —— 审批通过
# ============================================================================

class TestTicketApproveView(UsersAPIExtraBase):
    """审批通过：404 / 状态机 / 权限 / 成功执行授权写入"""

    def _grant_sa_role(self):
        _grant_global_role(self.super_admin, 'super_admin')

    @pytest.mark.integration
    def test_approve_not_found_404(self):
        """审批不存在的工单 → 404"""
        resp = self.client.post(
            '/api/v1/auth/permissions/tickets/999999/approve/',
            data=json.dumps({}), content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_approve_non_pending_400(self):
        """审批非 PENDING 工单 → 400"""
        self._grant_sa_role()
        t = self._create_pending_ticket()
        t.status = TicketStatus.CANCELLED
        t.save(update_fields=['status', 'updated_at'])
        resp = self.client.post(
            f'/api/v1/auth/permissions/tickets/{t.id}/approve/',
            data=json.dumps({}), content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_approve_without_permission_403(self):
        """无审批权限的用户审批 → 403（_can_approve_for_role 拒绝）"""
        t = self._create_pending_ticket()
        resp = self.client.post(
            f'/api/v1/auth/permissions/tickets/{t.id}/approve/',
            data=json.dumps({}), content_type='application/json', **self.normal_headers,
        )
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_approve_success_executes_grant(self):
        """超管审批通过 → 200，工单流转为 EXECUTED，目标用户获得团队 viewer 授权"""
        self._grant_sa_role()
        t = self._create_pending_ticket()
        resp = self.client.post(
            f'/api/v1/auth/permissions/tickets/{t.id}/approve/',
            data=json.dumps({'comment': '同意'}), content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['status'] == 'EXECUTED'
        t.refresh_from_db()
        assert t.status == TicketStatus.EXECUTED
        assert UserTeamScopeRel.objects.filter(
            user=t.target_user, role=t.role, team_id=t.scope_id,
            status=GrantStatus.ACTIVE,
        ).exists()


# ============================================================================
# TicketRejectView —— 驳回
# ============================================================================

class TestTicketRejectView(UsersAPIExtraBase):
    """驳回工单：理由必填 / 404 / 状态机 / 权限 / 成功驳回"""

    def _grant_sa_role(self):
        _grant_global_role(self.super_admin, 'super_admin')

    @pytest.mark.integration
    def test_reject_empty_comment_400(self):
        """驳回理由为空 → 400（comment 必填）"""
        self._grant_sa_role()
        t = self._create_pending_ticket()
        resp = self.client.post(
            f'/api/v1/auth/permissions/tickets/{t.id}/reject/',
            data=json.dumps({'comment': '   '}), content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_reject_not_found_404(self):
        """驳回不存在的工单 → 404"""
        resp = self.client.post(
            '/api/v1/auth/permissions/tickets/999999/reject/',
            data=json.dumps({'comment': 'x'}), content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_reject_non_pending_400(self):
        """驳回非 PENDING 工单 → 400"""
        self._grant_sa_role()
        t = self._create_pending_ticket()
        t.status = TicketStatus.APPROVED
        t.save(update_fields=['status', 'updated_at'])
        resp = self.client.post(
            f'/api/v1/auth/permissions/tickets/{t.id}/reject/',
            data=json.dumps({'comment': 'x'}), content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_reject_without_permission_403(self):
        """无审批权限的用户驳回 → 403"""
        t = self._create_pending_ticket()
        resp = self.client.post(
            f'/api/v1/auth/permissions/tickets/{t.id}/reject/',
            data=json.dumps({'comment': '驳回'}), content_type='application/json', **self.normal_headers,
        )
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_reject_success(self):
        """超管驳回 → 200，工单流转为 REJECTED（一票否决终态）"""
        self._grant_sa_role()
        t = self._create_pending_ticket()
        resp = self.client.post(
            f'/api/v1/auth/permissions/tickets/{t.id}/reject/',
            data=json.dumps({'comment': '资料不全'}), content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['status'] == 'REJECTED'
        t.refresh_from_db()
        assert t.status == TicketStatus.REJECTED


# ============================================================================
# AssignableRolesView —— 可申请角色清单
# ============================================================================

class TestAssignableRolesView(UsersAPIExtraBase):
    """可申请角色清单：按类别分组、排除 super_admin、scope_type 筛选"""

    @pytest.mark.integration
    def test_assignable_roles_200(self):
        """GET 返回自助申请清单 → 200，仅协作角色，按 rank 升序"""
        resp = self.client.get('/api/v1/auth/permissions/assignable-roles/', **self.normal_headers)
        assert resp.status_code == 200
        rows = resp.json()['rows']
        keys = {r['role_key'] for r in rows}
        # 管理岗(team_leader/dept_manager 等)一律走上级发起任命,不开放自助申请
        assert keys == {'viewer', 'contributor'}
        ranks = [r['rank'] for r in rows]
        assert ranks == sorted(ranks)

    @pytest.mark.integration
    def test_management_roles_returns_management(self):
        """purpose=management → 返回管理岗清单(供管理端发起任命)"""
        resp = self.client.get(
            '/api/v1/auth/permissions/assignable-roles/?purpose=management', **self.normal_headers)
        rows = resp.json()['rows']
        keys = {r['role_key'] for r in rows}
        assert {'team_leader', 'dept_manager'} <= keys
        assert 'super_admin' not in keys
        assert 'viewer' not in keys  # 协作角色不属于管理岗清单

    @pytest.mark.integration
    def test_scope_type_filter(self):
        """scope_type=TEAM 只返回团队类别角色"""
        resp = self.client.get(
            '/api/v1/auth/permissions/assignable-roles/?scope_type=TEAM', **self.normal_headers)
        rows = resp.json()['rows']
        assert rows and all(r['category'] == 'team' for r in rows)

    @pytest.mark.integration
    def test_anonymous_401(self):
        """匿名访问 → 401"""
        resp = self.client.get('/api/v1/auth/permissions/assignable-roles/')
        assert resp.status_code in (401, 403)


# ============================================================================
# ApprovalChainPreviewView —— 审批链预览
# ============================================================================

class TestApprovalChainPreviewView(UsersAPIExtraBase):
    """审批链预览：参数校验 + 降级链构造（不创建工单）"""

    @pytest.mark.integration
    def test_missing_role_key_400(self):
        """缺少 role_key → 400"""
        resp = self.client.get(
            '/api/v1/auth/permissions/approval-chain-preview/?scope_type=TEAM&scope_id=1',
            **self.normal_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_invalid_scope_type_400(self):
        """scope_type 取值非法 → 400"""
        resp = self.client.get(
            f'/api/v1/auth/permissions/approval-chain-preview/'
            f'?role_key=viewer&scope_type=XXX&scope_id={self.team_b.id}',
            **self.normal_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_scope_id_required_400(self):
        """scope_type=TEAM 时必须提供 scope_id → 400"""
        resp = self.client.get(
            '/api/v1/auth/permissions/approval-chain-preview/?role_key=viewer&scope_type=TEAM',
            **self.normal_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_role_not_found_400(self):
        """预览不存在的角色 → 400"""
        resp = self.client.get(
            f'/api/v1/auth/permissions/approval-chain-preview/'
            f'?role_key=ghost&scope_type=TEAM&scope_id={self.team_b.id}',
            **self.normal_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_invalid_scope_id_400(self):
        """scope_id 非整数 → 400"""
        resp = self.client.get(
            '/api/v1/auth/permissions/approval-chain-preview/?role_key=viewer&scope_type=TEAM&scope_id=abc',
            **self.normal_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_preview_success(self):
        """viewer 跨部门预览 → 200，目标部门经理缺失时返回降级后的超管单审链"""
        resp = self.client.get(
            f'/api/v1/auth/permissions/approval-chain-preview/'
            f'?role_key=viewer&scope_type=TEAM&scope_id={self._team_c().id}',
            **self.normal_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data['total_steps'] == 1
        assert data['chain'][0]['approver_role'] == 'SUPER_ADMIN'  # 目标部门经理缺失 → 降级
        assert data['scope_name'] == '市场一组'

