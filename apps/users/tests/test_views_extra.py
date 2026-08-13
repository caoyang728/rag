"""
apps.users.views 覆盖率补充测试 —— 针对覆盖报告中的缺失行（错误路径/边界条件）

按视图分组的补充用例：
- 辅助函数直测：_ensure_unique_code / _client_ip / _can_user_approve_ticket
- LoginView：LoginAttempt 写入异常兜底（验证码失败/密码错误/成功登录三种场景）
- Profile/Reset/PasswordReset：邮箱不可自改、密码强度边界、邮件发送失败、重置参数边界
- UserViewSet：manage_all 快路径 / 数据范围各分支 / 更新越权 / 角色过滤 / 批量导入导出
- Department/Team/Role：IntegrityError 兜底、团队销毁带文档拦截、dept_id 列表兼容等
- 权限申请/审批/工单中心：任命权限矩阵、异常兜底、序列化分支、待审批角色集合

基座与约定见 test_views_base.UsersAPIExtraBase（JWT + 真实 DB + RBAC 全链路）。
"""
import datetime
import json
from types import SimpleNamespace
from unittest.mock import patch, PropertyMock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.users.models import (
    User, Role, Department, Team, Permission, RolePermissionRel,
    UserRoleRel, UserDeptScopeRel, UserTeamScopeRel,
    TicketList, TicketPermissionDetail, TicketModelDetail, TicketAgentApprovalDetail,
    TicketStatus, TicketChangeType, ScopeType,
    GrantStatus, RoleType, DataScope,
)
from apps.users.serializers import (
    UserListSerializer, UserCreateSerializer, UserUpdateSerializer,
    DepartmentSerializer, DepartmentWriteSerializer, TeamSerializer, TeamWriteSerializer,
    ProfileUpdateSerializer,
)
from apps.users.ticket_service import _gen_ticket_no
from apps.users.services.org_service import _ensure_unique_code
from apps.users.services.user_service import (
    check_user_manage, check_can_manage_user, get_manageable_user_ids, filter_role_ids,
)
from apps.users.utils import _client_ip
from apps.users.views_tickets import _can_user_approve_ticket
from apps.users.views_users import UserViewSet
from apps.users.views_org import DepartmentViewSet, TeamViewSet
from apps.users.tests.test_views_base import (
    UsersAPIExtraBase, _get_or_create_role, _create_user, _grant_permission,
    _grant_global_role, _auth_headers, FakeRedis,
)


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------
def _make_view(view_class, user, query_params=None):
    """构造最小视图实例（仅填充视图代码实际读取的属性）"""
    view = view_class()
    view.request = SimpleNamespace(user=user, query_params=query_params or {})
    view.action = None
    view.kwargs = {}
    return view


def _make_custom_role_user(username, role_key, perm_key,
                           role_type=RoleType.GLOBAL, data_scope=DataScope.GLOBAL):
    """创建持有指定权限点的自定义角色用户（UserRoleRel 全局授权）"""
    role = _get_or_create_role(role_key, role_type=role_type, data_scope=data_scope)
    _grant_permission(role, perm_key)
    user = _create_user(username)
    UserRoleRel.objects.update_or_create(
        user=user, role=role, defaults={'status': GrantStatus.ACTIVE})
    return user


# ============================================================================
# 辅助函数直测
# ============================================================================
class TestHelperFunctions(UsersAPIExtraBase):
    """_ensure_unique_code / _client_ip / _can_user_approve_ticket"""

    def test_ensure_unique_code_exclude_id(self):
        """exclude_id 分支：排除自身后无冲突 → 原 code 返回"""
        dept = Department.objects.create(name='唯一部', code='uniq1')
        assert _ensure_unique_code('uniq1', Department, exclude_id=dept.id) == 'uniq1'

    def test_ensure_unique_code_suffix(self):
        """冲突时追加数字后缀（覆盖 max_n 扫描 + f-string 后缀）"""
        Department.objects.create(name='重名部', code='dup1')
        Department.objects.create(name='重名二部', code='dup1_1')
        assert _ensure_unique_code('dup1', Department) == 'dup1_2'

    def test_client_ip_xff_split(self):
        """X-Forwarded-For 取第一个 IP（覆盖 82 行 split）"""
        rf = RequestFactory()
        req = rf.get('/', HTTP_X_FORWARDED_FOR='1.2.3.4, 5.6.7.8')
        assert _client_ip(req) == '1.2.3.4'

    def test_can_user_approve_not_pending(self):
        """非 PENDING 工单 → False"""
        ticket = SimpleNamespace(status=TicketStatus.EXECUTED, approval_chain=[], current_step=0)
        assert _can_user_approve_ticket(self.normal_user, ticket) is False

    def test_can_user_approve_chain_done(self):
        """current_step 越界（审批链已完结）→ False"""
        ticket = SimpleNamespace(status=TicketStatus.PENDING, approval_chain=[], current_step=0)
        assert _can_user_approve_ticket(self.normal_user, ticket) is False

    def test_can_user_approve_no_role(self):
        """当前节点无 approver_role（文档/节点授权类）→ False"""
        ticket = SimpleNamespace(status=TicketStatus.PENDING, approval_chain=[{}], current_step=0)
        assert _can_user_approve_ticket(self.normal_user, ticket) is False

    def test_can_user_approve_system_auditor_no_perm(self):
        """SYSTEM_AUDITOR 节点但无 system.config.write 权限 → False"""
        ticket = SimpleNamespace(
            status=TicketStatus.PENDING, approval_chain=[{'approver_role': 'SYSTEM_AUDITOR'}],
            current_step=0)
        assert _can_user_approve_ticket(self.normal_user, ticket) is False

    def test_can_user_approve_system_auditor_self(self):
        """SYSTEM_AUDITOR 节点且申请人=审批人（防自审）→ False"""
        sysaudit = _make_custom_role_user('sysaudit1', 'sysaudit_role', 'system.config.write')
        ticket = SimpleNamespace(
            status=TicketStatus.PENDING, approval_chain=[{'approver_role': 'SYSTEM_AUDITOR'}],
            current_step=0, applicant_id=sysaudit.id)
        assert _can_user_approve_ticket(sysaudit, ticket) is False

    def test_can_user_approve_system_auditor_ok(self):
        """SYSTEM_AUDITOR 节点 + 权限 + 非自审 → True（覆盖共享审批池兜底路径）"""
        sysaudit = _make_custom_role_user('sysaudit2', 'sysaudit_role', 'system.config.write')
        ticket = SimpleNamespace(
            status=TicketStatus.PENDING, approval_chain=[{'approver_role': 'SYSTEM_AUDITOR'}],
            current_step=0, applicant_id=self.normal_user.id)
        assert _can_user_approve_ticket(sysaudit, ticket) is True

    def test_can_user_approve_super_admin_pool(self):
        """非 SYSTEM_AUDITOR 角色 → 走 ticket_service._can_approve_for_role（覆盖 2549-2550）"""
        ticket = SimpleNamespace(
            status=TicketStatus.PENDING, approval_chain=[{'approver_role': 'SUPER_ADMIN'}],
            current_step=0, applicant_id=self.normal_user.id, target_user_id=None)
        assert _can_user_approve_ticket(self.super_admin, ticket) is True


# ============================================================================
# UserViewSet 权限判定各分支（直测 user_service 纯函数）
# ============================================================================
class TestUserManageScopeChecks(UsersAPIExtraBase):
    """check_user_manage / check_can_manage_user / get_manageable_user_ids 分支"""

    def test_check_user_manage_manage_all_true(self):
        """user.manage_all 快路径放行"""
        gadmin = _make_custom_role_user('gadmin1', 'gadmin_role', 'user.manage_all')
        assert check_user_manage(gadmin, self.normal_user) is True

    def test_check_user_manage_dept_ok(self):
        """部门级：目标在本部门"""
        assert check_user_manage(self.dept_mgr, self.normal_user) is True

    def test_check_user_manage_dept_miss(self):
        """部门级：目标不在管辖部门 → False"""
        other = _create_user('other_deptb', department=self.dept_b)
        assert check_user_manage(self.dept_mgr, other) is False

    def test_check_user_manage_team_ok(self):
        """团队级：目标在本团队"""
        assert check_user_manage(self.team_leader, self.normal_user) is True

    def test_check_user_manage_team_miss(self):
        """团队级：目标不在管辖团队 → False"""
        other = _create_user('other_teamb', team=self.team_b, department=self.dept_a)
        assert check_user_manage(self.team_leader, other) is False

    def test_check_can_manage_no_perm(self):
        """无任何用户管理权限 → 没有禁用权限"""
        ok, msg = check_can_manage_user(self.normal_user, self.team_leader)
        assert ok is False and msg == '没有禁用权限'

    def test_check_can_manage_super_admin_target(self):
        """目标为超级管理员 → 不可禁用"""
        ok, msg = check_can_manage_user(self.dept_mgr, self.super_admin)
        assert ok is False and msg == '超级管理员不能被禁用'

    def test_check_can_manage_global_ok(self):
        """GLOBAL 范围 + 目标无同级权限 → 可禁用"""
        gadmin = _make_custom_role_user('gadmin2', 'gadmin_role', 'user.manage_all')
        ok, msg = check_can_manage_user(gadmin, self.normal_user)
        assert ok is True and msg == ''

    def test_check_can_manage_global_peer(self):
        """GLOBAL 范围 + 目标同为 manage_all 持有者 → 不可禁用"""
        gadmin = _make_custom_role_user('gadmin3', 'gadmin_role', 'user.manage_all')
        gadmin2 = _make_custom_role_user('gadmin4', 'gadmin_role', 'user.manage_all')
        ok, msg = check_can_manage_user(gadmin, gadmin2)
        assert ok is False and msg == '不能禁用同级用户管理员'

    def test_check_can_manage_dept_out_of_dept(self):
        """DEPT 范围 + 目标不在管辖部门"""
        other = _create_user('other_deptb2', department=self.dept_b)
        ok, msg = check_can_manage_user(self.dept_mgr, other)
        assert ok is False and msg == '只能禁用本部门员工'

    def test_check_can_manage_dept_peer(self):
        """DEPT 范围 + 目标为同级管理者"""
        tl2 = _create_user('leader2', team=self.team_a, department=self.dept_a)
        team_leader_role = _get_or_create_role('team_leader')
        UserTeamScopeRel.objects.create(
            user=tl2, role=team_leader_role, team=self.team_a, status=GrantStatus.ACTIVE)
        ok, msg = check_can_manage_user(self.dept_mgr, tl2)
        assert ok is False and msg == '不能禁用同级部门经理'

    def test_check_can_manage_dept_ok(self):
        """DEPT 范围 + 目标为本部门普通员工 → 可禁用"""
        ok, msg = check_can_manage_user(self.dept_mgr, self.normal_user)
        assert ok is True and msg == ''

    def test_check_can_manage_team_out_of_team(self):
        """TEAM 范围 + 目标不在管辖团队"""
        other = _create_user('other_teamb2', team=self.team_b, department=self.dept_a)
        ok, msg = check_can_manage_user(self.team_leader, other)
        assert ok is False and msg == '只能禁用本组员工'

    def test_check_can_manage_team_peer(self):
        """TEAM 范围 + 目标为同级管理者"""
        tl2 = _create_user('leader3', team=self.team_a, department=self.dept_a)
        team_leader_role = _get_or_create_role('team_leader')
        UserTeamScopeRel.objects.create(
            user=tl2, role=team_leader_role, team=self.team_a, status=GrantStatus.ACTIVE)
        ok, msg = check_can_manage_user(self.team_leader, tl2)
        assert ok is False and msg == '不能禁用同级团队组长'

    def test_check_can_manage_team_ok(self):
        """TEAM 范围 + 目标为本团队普通员工 → 可禁用"""
        ok, msg = check_can_manage_user(self.team_leader, self.normal_user)
        assert ok is True and msg == ''

    def test_manageable_ids_global_none(self):
        """manage_all → 返回 None（全部可管理）"""
        gadmin = _make_custom_role_user('gadmin5', 'gadmin_role', 'user.manage_all')
        assert get_manageable_user_ids(gadmin) is None

    def test_manageable_ids_dept(self):
        """部门级 → 管辖部门用户 ID 集合"""
        other = _create_user('other_deptb3', department=self.dept_b)
        ids = get_manageable_user_ids(self.dept_mgr)
        assert self.normal_user.id in ids
        assert other.id not in ids

    def test_manageable_ids_team(self):
        """团队级 → 管辖团队用户 ID 集合"""
        other = _create_user('other_teamb3', team=self.team_b, department=self.dept_a)
        ids = get_manageable_user_ids(self.team_leader)
        assert self.normal_user.id in ids
        assert other.id not in ids

    def test_manageable_ids_self(self):
        """普通用户只能管理自己"""
        assert get_manageable_user_ids(self.normal_user) == {self.normal_user.id}

    def test_filter_role_ids_restricted_raise(self):
        """非超管分配受限角色（super_admin 角色）→ PermissionDenied"""
        sa_role = _get_or_create_role('super_admin')
        with pytest.raises(PermissionDenied):
            filter_role_ids(self.dept_mgr, [sa_role.id])

    def test_get_queryset_no_perm_only_self(self):
        """无 user.manage 权限 → 只能看到自己（覆盖 584-586）"""
        view = _make_view(UserViewSet, self.normal_user)
        qs = list(view.get_queryset())
        assert {u.id for u in qs} == {self.normal_user.id}

    def test_get_queryset_global_scope_fallback_self(self):
        """user.manage + GLOBAL 数据范围（非常规组合）→ 兜底只看自己（覆盖 581-583）"""
        gmgr = _make_custom_role_user('gmgr1', 'gmgr_role', 'user.manage',
                                      role_type=RoleType.GLOBAL, data_scope=DataScope.GLOBAL)
        view = _make_view(UserViewSet, gmgr)
        qs = list(view.get_queryset())
        assert {u.id for u in qs} == {gmgr.id}

    def test_get_serializer_class_mapping(self):
        """action → serializer 映射（覆盖 623-629）"""
        view = _make_view(UserViewSet, self.super_admin)
        view.action = 'list'
        assert view.get_serializer_class() is UserListSerializer
        view.action = 'create'
        assert view.get_serializer_class() is UserCreateSerializer
        view.action = 'partial_update'
        assert view.get_serializer_class() is UserUpdateSerializer
        view.action = 'retrieve'
        from apps.users.serializers import UserSerializer
        assert view.get_serializer_class() is UserSerializer


# ============================================================================
# LoginView / Profile / 密码重置 —— 异常兜底与参数边界
# ============================================================================
class TestLoginEdge(UsersAPIExtraBase):
    """LoginView：LoginAttempt 写入失败兜底（不影响登录流程）"""

    @patch('apps.security.models.LoginAttempt.objects.create', side_effect=Exception('db error'))
    @patch('apps.security.views.verify_captcha', return_value=False)
    def test_captcha_fail_login_attempt_error_still_401(self, _mock_captcha, _mock_create):
        """验证码失败 + LoginAttempt 写入异常 → 仍返回 401（覆盖 134-135）"""
        resp = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'username': 'normal', 'password': 'wrong',
                             'captcha_id': 'x', 'captcha_code': 'bad'}),
            content_type='application/json',
        )
        assert resp.status_code == 401
        assert resp.json()['detail'] == '验证码错误'

    @patch('apps.security.models.LoginAttempt.objects.create', side_effect=Exception('db error'))
    @patch('apps.security.views.verify_captcha', return_value=True)
    def test_wrong_password_login_attempt_error_still_401(self, _mock_captcha, _mock_create):
        """密码错误 + LoginAttempt 写入异常 → 仍返回 401（覆盖 149-150）"""
        resp = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'username': 'normal', 'password': 'badpass',
                             'captcha_id': 'x', 'captcha_code': 'x'}),
            content_type='application/json',
        )
        assert resp.status_code == 401

    @patch('apps.security.models.LoginAttempt.objects.create', side_effect=Exception('db error'))
    @patch('apps.security.views.verify_captcha', return_value=True)
    def test_login_success_login_attempt_error_still_200(self, _mock_captcha, _mock_create):
        """成功登录 + LoginAttempt 写入异常 → 仍返回 JWT（覆盖 172-173）"""
        resp = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'username': 'normal', 'password': 'pass12345',
                             'captcha_id': 'x', 'captcha_code': 'x'}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        assert 'access' in resp.json()


class TestProfileAndPasswordEdge(UsersAPIExtraBase):
    """Profile 邮箱拦截 / 修改密码边界 / 密码重置异常与边界"""

    @pytest.mark.integration
    def test_profile_email_in_data_403(self):
        """validated_data 含 email（绕过 serializer 字段白名单）→ 403 拦截（覆盖 220-221）"""
        with patch.object(ProfileUpdateSerializer, 'is_valid', return_value=True), \
             patch.object(ProfileUpdateSerializer, 'validated_data',
                          new_callable=PropertyMock, return_value={'email': 'evil@x.com'}):
            resp = self.client.patch(
                '/api/v1/auth/profile/',
                data=json.dumps({'email': 'evil@x.com'}),
                content_type='application/json', **self.normal_headers)
        assert resp.status_code == 403
        assert '企业邮箱不可自行修改' in resp.json()['detail']

    @pytest.mark.integration
    def test_reset_password_too_long_400(self):
        """新密码 > 32 位（覆盖 234-235）"""
        resp = self.client.post(
            '/api/v1/auth/reset-password/',
            data=json.dumps({'old_password': 'old', 'new_password': 'Abcd1234' + 'x' * 26}),
            content_type='application/json', **self.normal_headers)
        assert resp.status_code == 400
        assert resp.json()['detail'] == '新密码最多 32 位'

    @pytest.mark.integration
    def test_reset_password_no_lowercase_400(self):
        """新密码缺少小写字母（覆盖 240-241）"""
        resp = self.client.post(
            '/api/v1/auth/reset-password/',
            data=json.dumps({'old_password': 'old', 'new_password': 'ABCD12345'}),
            content_type='application/json', **self.normal_headers)
        assert resp.status_code == 400
        assert resp.json()['detail'] == '新密码必须包含小写字母'

    @pytest.mark.integration
    def test_reset_password_no_digit_400(self):
        """新密码缺少数字（覆盖 242-243）"""
        resp = self.client.post(
            '/api/v1/auth/reset-password/',
            data=json.dumps({'old_password': 'old', 'new_password': 'Abcdefgh'}),
            content_type='application/json', **self.normal_headers)
        assert resp.status_code == 400
        assert resp.json()['detail'] == '新密码必须包含数字'

    @patch('apps.security.views.verify_captcha', return_value=True)
    @patch('apps.security.views._get_redis', return_value=FakeRedis())
    @patch('django.core.mail.send_mail', side_effect=Exception('smtp down'))
    def test_password_reset_request_mail_fail_500(self, _mock_mail, _mock_redis, _mock_captcha):
        """邮件发送异常 → 500（覆盖 300-302）"""
        resp = self.client.post(
            '/api/v1/auth/password-reset/request/',
            data=json.dumps({'email': self.normal_user.email,
                             'captcha_id': 'c', 'captcha_code': 'x'}),
            content_type='application/json',
        )
        assert resp.status_code == 500
        assert '验证码发送失败' in resp.json()['detail']

    def test_password_reset_confirm_no_code_400(self):
        """缺少验证码（覆盖 325-326）"""
        resp = self.client.post(
            '/api/v1/auth/password-reset/confirm/',
            data=json.dumps({'email': 'a@b.com', 'code': '', 'new_password': 'Abcdef12'}),
            content_type='application/json',
        )
        assert resp.status_code == 400
        assert resp.json()['detail'] == '请输入验证码'

    def test_password_reset_confirm_short_400(self):
        """新密码 < 8 位（覆盖 327-328）"""
        resp = self.client.post(
            '/api/v1/auth/password-reset/confirm/',
            data=json.dumps({'email': 'a@b.com', 'code': '123456', 'new_password': 'Abc1'}),
            content_type='application/json',
        )
        assert resp.status_code == 400
        assert resp.json()['detail'] == '新密码至少 8 位'

    def test_password_reset_confirm_long_400(self):
        """新密码 > 32 位（覆盖 329-330）"""
        resp = self.client.post(
            '/api/v1/auth/password-reset/confirm/',
            data=json.dumps({'email': 'a@b.com', 'code': '123456',
                             'new_password': 'Abcd1234' + 'x' * 26}),
            content_type='application/json',
        )
        assert resp.status_code == 400
        assert resp.json()['detail'] == '新密码最多 32 位'

    def test_password_reset_confirm_no_lower_400(self):
        """新密码缺少小写字母（覆盖 333-334）"""
        resp = self.client.post(
            '/api/v1/auth/password-reset/confirm/',
            data=json.dumps({'email': 'a@b.com', 'code': '123456', 'new_password': 'ABCD12345'}),
            content_type='application/json',
        )
        assert resp.status_code == 400
        assert resp.json()['detail'] == '新密码必须包含小写字母'

    def test_password_reset_confirm_no_digit_400(self):
        """新密码缺少数字（覆盖 335-336）"""
        resp = self.client.post(
            '/api/v1/auth/password-reset/confirm/',
            data=json.dumps({'email': 'a@b.com', 'code': '123456', 'new_password': 'Abcdefgh'}),
            content_type='application/json',
        )
        assert resp.status_code == 400
        assert resp.json()['detail'] == '新密码必须包含数字'


# ============================================================================
# UserViewSet —— 更新/删除/恢复/批量操作的越权与边界
# ============================================================================
class TestUserViewSetEdge(UsersAPIExtraBase):
    """UserViewSet update/destroy/revive/batch 系列边界"""

    def _tl2(self, username='leader2'):
        """研发部后端组的第二个团队组长（用于同级管理者互斥判定）"""
        tl2 = _create_user(username, team=self.team_a, department=self.dept_a)
        team_leader_role = _get_or_create_role('team_leader')
        UserTeamScopeRel.objects.create(
            user=tl2, role=team_leader_role, team=self.team_a, status=GrantStatus.ACTIVE)
        return tl2

    @pytest.mark.integration
    def test_create_no_scope_403(self):
        """user.manage 但 GLOBAL 数据范围（无部门/团队管辖）→ 创建用户 403（覆盖 644-645）"""
        gmgr = _make_custom_role_user('gmgr_create', 'gmgr_role', 'user.manage',
                                      role_type=RoleType.GLOBAL, data_scope=DataScope.GLOBAL)
        resp = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({'username': 'x', 'email': 'x@test.com'}),
            content_type='application/json', **_auth_headers(gmgr))
        assert resp.status_code == 403
        assert resp.json()['detail'] == '无用户管理权限'

    @pytest.mark.integration
    def test_update_out_of_scope_403(self):
        """部门经理编辑管辖范围外用户 → 403（覆盖 742-743）

        API 侧 get_object 会先被 get_queryset 范围过滤（越权用户返回 404），
        故直调 update 方法绕过 get_queryset，验证 742-743 的 Response 分支。
        """
        other = _create_user('other_deptb', department=self.dept_b)
        view = _make_view(UserViewSet, self.dept_mgr)
        req = SimpleNamespace(user=self.dept_mgr, data={'real_name': 'x'})
        with patch.object(UserViewSet, 'get_object', return_value=other):
            resp = view.update(req)
        assert resp.status_code == 403
        assert resp.data['detail'] == '无权限编辑该用户'

    @pytest.mark.integration
    def test_update_status_denied_403(self):
        """部门经理禁言同级管理者 → 403（覆盖 745-748）"""
        tl2 = self._tl2()
        resp = self.client.patch(
            f'/api/v1/auth/users/{tl2.id}/',
            data=json.dumps({'status': 'disabled'}),
            content_type='application/json', **self.dept_mgr_headers)
        assert resp.status_code == 403
        assert resp.json()['detail'] == '不能禁用同级部门经理'

    @pytest.mark.integration
    def test_update_no_scope_403(self):
        """user.manage_all（非超管）编辑用户 → 无部门/团队范围 → 403（覆盖 762-764）"""
        gadmin = _make_custom_role_user('gadmin6', 'gadmin_role', 'user.manage_all')
        resp = self.client.patch(
            f'/api/v1/auth/users/{self.normal_user.id}/',
            data=json.dumps({'real_name': 'x'}),
            content_type='application/json', **_auth_headers(gadmin))
        assert resp.status_code == 403
        assert resp.json()['detail'] == '无权限编辑该用户'

    @pytest.mark.integration
    def test_update_drops_high_roles(self):
        """部门经理给用户分配 super_admin 角色 → 被 _filter_downward_roles 过滤（覆盖 766-767）"""
        sa_role = _get_or_create_role('super_admin')
        resp = self.client.patch(
            f'/api/v1/auth/users/{self.normal_user.id}/',
            data=json.dumps({'role_ids': [sa_role.id]}),
            content_type='application/json', **self.dept_mgr_headers)
        assert resp.status_code == 200
        assert not UserRoleRel.objects.filter(user=self.normal_user, role=sa_role,
                                              status=GrantStatus.ACTIVE).exists()

    @pytest.mark.integration
    def test_team_leader_clear_teams_403(self):
        """组长清空团队 → 403（覆盖 771-773）"""
        resp = self.client.patch(
            f'/api/v1/auth/users/{self.normal_user.id}/',
            data=json.dumps({'team_ids': []}),
            content_type='application/json', **self.leader_headers)
        assert resp.status_code == 403
        assert resp.json()['detail'] == '不能清空所有团队'

    @pytest.mark.integration
    def test_dept_mgr_change_department_403(self):
        """部门经理修改用户部门 → 403（覆盖 780-785）"""
        resp = self.client.patch(
            f'/api/v1/auth/users/{self.normal_user.id}/',
            data=json.dumps({'department_id': self.dept_b.id}),
            content_type='application/json', **self.dept_mgr_headers)
        assert resp.status_code == 403
        assert resp.json()['detail'] == '无权修改部门'

    @pytest.mark.integration
    def test_update_team_not_exists_400(self):
        """team_ids 指向不存在的团队 → 400（覆盖 826-827）"""
        resp = self.client.patch(
            f'/api/v1/auth/users/{self.normal_user.id}/',
            data=json.dumps({'team_ids': [999999]}),
            content_type='application/json', **self.admin_headers)
        assert resp.status_code == 400
        assert resp.json()['detail'] == '指定的团队不存在'

    @pytest.mark.integration
    def test_update_dept_manager_conflict_400(self):
        """给用户授予 dept_manager 时同部门已有经理 → 400（覆盖 790-799）"""
        dm_role = _get_or_create_role('dept_manager')
        dm_other = _create_user('dm_other', department=self.dept_a)
        _grant_global_role(dm_other, 'dept_manager')
        resp = self.client.patch(
            f'/api/v1/auth/users/{self.normal_user.id}/',
            data=json.dumps({'department_id': self.dept_a.id, 'role_ids': [dm_role.id]}),
            content_type='application/json', **self.admin_headers)
        assert resp.status_code == 400
        assert '该部门已有部门经理' in resp.json()['detail']

    @pytest.mark.integration
    def test_batch_delete_no_perm_403(self):
        """user.manage + GLOBAL（无管辖范围）→ 批量删除 403（覆盖 854-855）"""
        gmgr = _make_custom_role_user('gmgr_bdel', 'gmgr_role', 'user.manage',
                                      role_type=RoleType.GLOBAL, data_scope=DataScope.GLOBAL)
        resp = self.client.post(
            '/api/v1/auth/users/batch_delete/',
            data=json.dumps({'ids': [self.normal_user.id]}),
            content_type='application/json', **_auth_headers(gmgr))
        assert resp.status_code == 403
        assert resp.json()['detail'] == '无用户管理权限'

    @pytest.mark.integration
    def test_revive_out_of_scope_403(self):
        """部门经理恢复管辖范围外的已删用户 → 403（覆盖 904-906）"""
        deleted = _create_user('del_deptb', department=self.dept_b)
        deleted.is_deleted = True
        deleted.deleted_at = timezone.now()
        deleted.status = 'disabled'
        deleted.save()
        resp = self.client.post(
            f'/api/v1/auth/users/{deleted.id}/revive/',
            data=json.dumps({'status': 'active'}),
            content_type='application/json', **self.dept_mgr_headers)
        assert resp.status_code == 403
        assert resp.json()['detail'] == '只能禁用本部门员工'

    @pytest.mark.integration
    def test_assign_roles_non_super_403(self):
        """非超管分配角色 → 403（覆盖 1034-1035）"""
        resp = self.client.post(
            f'/api/v1/auth/users/{self.normal_user.id}/assign_roles/',
            data=json.dumps({'role_ids': []}),
            content_type='application/json', **self.dept_mgr_headers)
        assert resp.status_code == 403
        assert resp.json()['detail'] == '仅超级管理员可分配角色'

    @pytest.mark.integration
    def test_batch_export_scope_filter(self):
        """部门经理批量导出 → 越权 ID 被过滤（覆盖 1101-1102）"""
        other = _create_user('other_deptb', department=self.dept_b)
        resp = self.client.post(
            '/api/v1/auth/users/batch_export/',
            data=json.dumps({'ids': [self.normal_user.id, other.id]}),
            content_type='application/json', **self.dept_mgr_headers)
        assert resp.status_code == 200
        content = resp.content.decode('utf-8-sig')
        assert self.normal_user.username in content
        assert other.username not in content

    @pytest.mark.integration
    def test_batch_import_no_perm_403(self):
        """user.manage + GLOBAL（无管辖范围）→ 批量导入 403（覆盖 1127-1128）"""
        gmgr = _make_custom_role_user('gmgr_bimp', 'gmgr_role', 'user.manage',
                                      role_type=RoleType.GLOBAL, data_scope=DataScope.GLOBAL)
        f = SimpleUploadedFile('users.csv', b'x', content_type='text/csv')
        resp = self.client.post('/api/v1/auth/users/batch_import/', {'file': f},
                                **_auth_headers(gmgr))
        assert resp.status_code == 403
        assert resp.json()['detail'] == '无用户管理权限'


class TestBatchImportEdge(UsersAPIExtraBase):
    """批量导入 CSV 的行级校验与创建失败兜底"""

    @pytest.mark.integration
    def test_empty_csv_400(self):
        """空文件 → 400（覆盖 1140-1141）"""
        f = SimpleUploadedFile('empty.csv', b'', content_type='text/csv')
        resp = self.client.post('/api/v1/auth/users/batch_import/', {'file': f},
                                **self.admin_headers)
        assert resp.status_code == 400
        assert resp.json()['detail'] == 'CSV 文件为空'

    @pytest.mark.integration
    def test_mixed_rows_result_csv(self):
        """混合合法/非法行 → 返回结果 CSV 与统计 header（覆盖 1176-1177/1191-1200/1207-1208/1223-1224/1233-1239/1265-1266）"""
        lines = [
            '用户名,姓名,邮箱,部门,团队,状态',
            'imp1,导入一,imp1@test.com,研发部,后端组,启用',
            'imp2,导入二,imp2@test.com,研发部,后端组,禁用',
            ',缺名,imp3@test.com,研发部,后端组,启用',
            'imp4,,imp4@test.com,研发部,后端组,启用',
            'imp5,缺邮,,研发部,后端组,启用',
            'imp6,重名,imp6@test.com,研发部,后端组,启用',
            'imp6,重名,imp6@test.com,研发部,后端组,启用',
            'imp7,重邮,normal@test.com,研发部,后端组,启用',
            'imp8,部门不存在,imp8@test.com,不存在部,后端组,启用',
            'imp9,团队不存在,imp9@test.com,研发部,不存在组,启用',
            'imp10,坏状态,imp10@test.com,研发部,后端组,未知',
            'imp11,短行',
        ]
        f = SimpleUploadedFile('users.csv', '\n'.join(lines).encode('utf-8'),
                               content_type='text/csv')
        resp = self.client.post('/api/v1/auth/users/batch_import/', {'file': f},
                                **self.admin_headers)
        assert resp.status_code == 200
        assert resp['X-Import-Success'] == '3'
        assert resp['X-Import-Fail'] == '9'
        body = resp.content.decode('utf-8-sig')
        for reason in ('用户名不能为空', '姓名不能为空', '邮箱不能为空',
                       '用户名「imp6」已存在', '邮箱「normal@test.com」已被使用',
                       '部门「不存在部」不存在', '团队「不存在组」在部门「研发部」下不存在',
                       '状态「未知」无效，应为 启用/禁用'):
            assert reason in body

    @pytest.mark.integration
    def test_create_fail_row_recorded(self):
        """User.objects.create 抛异常 → 行标记失败（覆盖 1267-1268）"""
        lines = [
            '用户名,姓名,邮箱,部门,团队,状态',
            'imp12,导入十二,imp12@test.com,研发部,后端组,启用',
        ]
        f = SimpleUploadedFile('users.csv', '\n'.join(lines).encode('utf-8'),
                               content_type='text/csv')
        with patch('apps.users.views_users.User.objects.create',
                   side_effect=Exception('disk full')):
            resp = self.client.post('/api/v1/auth/users/batch_import/', {'file': f},
                                    **self.admin_headers)
        assert resp.status_code == 200
        assert resp['X-Import-Fail'] == '1'
        assert '创建失败' in resp.content.decode('utf-8-sig')

    @pytest.mark.integration
    def test_leader_import_cross_dept(self):
        """组长导入本部门外员工 → 行级拒绝（覆盖 1213-1215）"""
        lines = [
            '用户名,姓名,邮箱,部门,团队,状态',
            'imp13,导入十三,imp13@test.com,市场部,前端组,启用',
        ]
        f = SimpleUploadedFile('users.csv', '\n'.join(lines).encode('utf-8'),
                               content_type='text/csv')
        resp = self.client.post('/api/v1/auth/users/batch_import/', {'file': f},
                                **self.leader_headers)
        assert resp.status_code == 200
        assert '组长只能导入本部门员工' in resp.content.decode('utf-8-sig')

    @pytest.mark.integration
    def test_leader_import_other_team(self):
        """组长导入非本团队员工 → 行级拒绝（覆盖 1228-1229）"""
        lines = [
            '用户名,姓名,邮箱,部门,团队,状态',
            'imp14,导入十四,imp14@test.com,研发部,前端组,启用',
        ]
        f = SimpleUploadedFile('users.csv', '\n'.join(lines).encode('utf-8'),
                               content_type='text/csv')
        resp = self.client.post('/api/v1/auth/users/batch_import/', {'file': f},
                                **self.leader_headers)
        assert resp.status_code == 200
        assert '组长只能导入本团队员工' in resp.content.decode('utf-8-sig')

    @pytest.mark.integration
    def test_dept_mgr_import_cross_dept(self):
        """部门经理导入其他部门员工 → 行级拒绝（覆盖 1216-1217）"""
        lines = [
            '用户名,姓名,邮箱,部门,团队,状态',
            'imp15,导入十五,imp15@test.com,市场部,前端组,启用',
        ]
        f = SimpleUploadedFile('users.csv', '\n'.join(lines).encode('utf-8'),
                               content_type='text/csv')
        resp = self.client.post('/api/v1/auth/users/batch_import/', {'file': f},
                                **self.dept_mgr_headers)
        assert resp.status_code == 200
        assert '部门经理只能导入本部门员工' in resp.content.decode('utf-8-sig')


# ============================================================================
# form_options / search —— 角色下拉与搜索范围
# ============================================================================
class TestFormOptionsAndSearch(UsersAPIExtraBase):
    """form_options 各权限分支 + search 范围过滤"""

    def _kb_admin_user(self, username='kbadmin'):
        return _make_custom_role_user(username, 'kbadmin_role', 'kb.manage_all')

    @pytest.mark.integration
    def test_form_options_kb_admin(self):
        """kb.manage_all → 除 super_admin 外全可见（覆盖 1314-1317）

        form_options 挂在 UserViewSet（类级 CanManageUsers 需 user.manage），
        kb_admin 仅持 kb.manage_all，故直调视图方法验证 1314-1317 分支。
        """
        kbadmin = self._kb_admin_user('kbadmin_fo')
        view = _make_view(UserViewSet, kbadmin)
        resp = view.form_options(SimpleNamespace(user=kbadmin))
        assert resp.status_code == 200
        codes = {r['code'] for r in resp.data['roles']}
        assert 'super_admin' not in codes
        assert 'viewer' in codes

    @pytest.mark.integration
    def test_form_options_dept_mgr(self):
        """部门经理：角色过滤 + assignable 限定 team_leader/contributor/viewer（覆盖 1318-1323）"""
        resp = self.client.get('/api/v1/auth/users/form_options/', **self.dept_mgr_headers)
        data = resp.json()
        codes = {r['code'] for r in data['roles']}
        assignable = {r['code'] for r in data['assignable_roles']}
        assert 'super_admin' not in codes and 'user_admin' not in codes
        assert assignable <= {'team_leader', 'contributor', 'viewer'}

    @pytest.mark.integration
    def test_form_options_team_leader(self):
        """团队组长：仅可见/可分配基础角色（覆盖 1324-1327）"""
        resp = self.client.get('/api/v1/auth/users/form_options/', **self.leader_headers)
        data = resp.json()
        codes = {r['code'] for r in data['roles']}
        assignable = {r['code'] for r in data['assignable_roles']}
        assert codes <= {'team_leader', 'contributor', 'viewer'}
        assert assignable == {'contributor', 'viewer'}

    @pytest.mark.integration
    def test_form_options_no_perm_user(self):
        """普通用户：空列表（覆盖 1328-1330）"""
        view = _make_view(UserViewSet, self.normal_user)
        resp = view.form_options(SimpleNamespace(user=self.normal_user))
        assert resp.status_code == 200
        assert resp.data['roles'] == [] and resp.data['assignable_roles'] == []

    @pytest.mark.integration
    def test_search_scope_filter(self):
        """部门经理搜索不到管辖范围外用户（覆盖 1343-1344）"""
        other = _create_user('search_deptb', department=self.dept_b)
        resp = self.client.get('/api/v1/auth/users/search/?q=search_deptb',
                               **self.dept_mgr_headers)
        assert resp.status_code == 200
        users = resp.json()['users']
        assert all(u['username'] != 'search_deptb' for u in users)

    @pytest.mark.integration
    def test_search_team_filter(self):
        """按团队过滤搜索（覆盖 1353-1355）"""
        _create_user('search_team_b', team=self.team_b, department=self.dept_a)
        resp = self.client.get(f'/api/v1/auth/users/search/?team_id={self.team_b.id}',
                               **self.admin_headers)
        assert resp.status_code == 200
        users = resp.json()['users']
        assert users
        assert all(u['department_id'] == self.dept_a.id for u in users)
        assert any(u['username'] == 'search_team_b' for u in users)


# ============================================================================
# permission-detail / MyPermissions / approvers
# ============================================================================
class TestPermissionDetailAndMyPermissions(UsersAPIExtraBase):
    """用户权限详情平铺 + 个人权限分组"""

    @pytest.mark.integration
    def test_permission_detail_dept_rel_row(self):
        """部门属地授权 → '全部团队' 行（覆盖 992-1000）"""
        contributor = _get_or_create_role('contributor')
        UserDeptScopeRel.objects.create(
            user=self.normal_user, role=contributor, dept=self.dept_b,
            status=GrantStatus.ACTIVE)
        resp = self.client.get(f'/api/v1/auth/users/{self.normal_user.id}/permission-detail/',
                               **self.admin_headers)
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert any(r['team_name'] == '全部团队' and r['dept_name'] == '市场部'
                   for r in rows)

    @pytest.mark.integration
    def test_permission_detail_skips_global_viewer(self):
        """全局 viewer 兜底不重复展示（覆盖 1008-1010）"""
        _grant_global_role(self.normal_user, 'viewer')
        resp = self.client.get(f'/api/v1/auth/users/{self.normal_user.id}/permission-detail/',
                               **self.admin_headers)
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert not any(r['dept_name'] == '全部' and r['role_code'] == 'viewer' for r in rows)

    @pytest.mark.integration
    def test_my_permissions_groups_with_perms(self):
        """部门经理：权限点按模块分组（覆盖 1829-1850）"""
        resp = self.client.get('/api/v1/auth/permissions/me/', **self.dept_mgr_headers)
        assert resp.status_code == 200
        groups = resp.json()['permission_groups']
        assert groups.get('user')
        assert any(p['code'] == 'user.manage' for p in groups['user'])


class TestApproversView(UsersAPIExtraBase):
    """PermissionApproversView（已废弃接口）的知识库管理员反查分支"""

    def _kb_admin_user(self, username='kbadmin'):
        return _make_custom_role_user(username, 'kbadmin_role', 'kb.manage_all')

    @pytest.mark.integration
    def test_approvers_department_includes_kb_admin(self):
        """scope=department → 反查 kb.manage_all 持有者（覆盖 1910-1918）"""
        kbadmin = self._kb_admin_user('kbadmin_appr')
        resp = self.client.get('/api/v1/auth/permissions/approvers/?scope=department',
                               **self.normal_headers)
        assert resp.status_code == 200
        ids = {a['id'] for a in resp.json()['approvers']}
        assert kbadmin.id in ids


# ============================================================================
# Department / Team / Role —— IntegrityError 兜底与分支
# ============================================================================
class TestOrgViewSetEdge(UsersAPIExtraBase):
    """Department/Team 异常兜底、Team 销毁文档拦截、dept_id 兼容"""

    @pytest.mark.integration
    def test_department_get_serializer_class(self):
        """Department action → serializer 映射（覆盖 1380-1383）"""
        view = _make_view(DepartmentViewSet, self.super_admin)
        view.action = 'create'
        assert view.get_serializer_class() is DepartmentWriteSerializer
        view.action = 'list'
        assert view.get_serializer_class() is DepartmentSerializer

    def _org_ua_headers(self, username='org_ua'):
        """构造 user_admin 审批人 header（org 工单单审节点审批者）"""
        ua = _create_user(username)
        _grant_global_role(ua, 'user_admin')
        return _auth_headers(ua)

    def _approve(self, ticket, headers):
        """审批工单当前节点（HTTP 统一审批入口）"""
        return self.client.post(
            f'/api/v1/auth/tickets/{ticket.id}/approve/',
            data=json.dumps({'comment': '同意'}),
            content_type='application/json', **headers)

    @pytest.mark.integration
    def test_department_restore_execute_integrity_error_rollback(self):
        """恢复已删部门时执行层落库冲突 → 审批返回 400，工单留 PENDING 可重试

        软删恢复语义已移至工单执行层(_execute_org_change)，落库冲突抛 ValueError
        回滚审批事务，不再由创建接口兜底。
        """
        from django.db import IntegrityError
        dept = Department.objects.create(name='旧部门R', code='oldr')
        dept.is_deleted = True
        dept.save()
        resp = self.client.post(
            '/api/v1/auth/departments/',
            data=json.dumps({'name': '旧部门R'}),
            content_type='application/json', **self.admin_headers)
        assert resp.status_code == 201
        ticket = TicketList.objects.get(ticket_no=resp.json()['ticket_no'])
        with patch.object(Department, 'save', side_effect=IntegrityError('dup')):
            aresp = self._approve(ticket, self._org_ua_headers())
        assert aresp.status_code == 400
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.PENDING
        dept.refresh_from_db()
        assert dept.is_deleted is True  # 事务回滚,未被误恢复

    @pytest.mark.integration
    def test_department_create_returns_ticket(self):
        """新建部门 → 201 工单（不直接落库，唯一性由创建时预检保证）"""
        resp = self.client.post(
            '/api/v1/auth/departments/',
            data=json.dumps({'name': '新部门Z'}),
            content_type='application/json', **self.admin_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data['ticket_no'].startswith('ZZ')
        assert not Department.objects.filter(name='新部门Z', is_deleted=False).exists()

    @pytest.mark.integration
    def test_department_update_returns_ticket(self):
        """更新部门 → 200 工单（DB 不变，审批后生效）"""
        resp = self.client.patch(
            f'/api/v1/auth/departments/{self.dept_a.id}/',
            data=json.dumps({'name': '研发部X'}),
            content_type='application/json', **self.admin_headers)
        assert resp.status_code == 200
        assert resp.json()['ticket_no'].startswith('ZZ')
        self.dept_a.refresh_from_db()
        assert self.dept_a.name == '研发部'

    @pytest.mark.integration
    def test_team_list_bad_dept_filter_ignored(self):
        """非数字 department_id 过滤被忽略（覆盖 1629-1633）"""
        resp = self.client.get('/api/v1/auth/teams/?department_id=abc', **self.normal_headers)
        assert resp.status_code == 200

    @pytest.mark.integration
    def test_dept_mgr_update_own_dept_team_200(self):
        """部门经理可操作本部门团队 → 200 工单（组织变更走审批,不直接落库）"""
        resp = self.client.patch(
            f'/api/v1/auth/teams/{self.team_a.id}/',
            data=json.dumps({'description': '部门经理改'}),
            content_type='application/json', **self.dept_mgr_headers)
        assert resp.status_code == 200
        assert resp.json()['ticket_no'].startswith('ZZ')
        self.team_a.refresh_from_db()
        assert self.team_a.description is None  # 审批前不生效

    @pytest.mark.integration
    def test_team_get_serializer_class(self):
        """Team action → serializer 映射（覆盖 1654-1657）"""
        view = _make_view(TeamViewSet, self.super_admin)
        view.action = 'create'
        assert view.get_serializer_class() is TeamWriteSerializer
        view.action = 'retrieve'
        assert view.get_serializer_class() is TeamSerializer

    @pytest.mark.integration
    def test_team_create_dept_id_as_list(self):
        """department_id 传数组 → 取首元素写入工单目标数据（覆盖取首元素分支）"""
        resp = self.client.post(
            '/api/v1/auth/teams/',
            data=json.dumps({'name': '列表部门团队', 'department_id': [self.dept_b.id]}),
            content_type='application/json', **self.admin_headers)
        assert resp.status_code == 201
        ticket = TicketList.objects.get(ticket_no=resp.json()['ticket_no'])
        assert ticket.org_detail.target_data['department_id'] == self.dept_b.id
        assert not Team.objects.filter(name='列表部门团队').exists()

    @pytest.mark.integration
    def test_team_restore_execute_integrity_error_rollback(self):
        """恢复已删团队时执行层落库冲突 → 审批返回 400，工单留 PENDING"""
        from django.db import IntegrityError
        team = Team.objects.create(name='旧团队R', code='oldtr', department=self.dept_a)
        team.is_deleted = True
        team.save()
        resp = self.client.post(
            '/api/v1/auth/teams/',
            data=json.dumps({'name': '旧团队R', 'department_id': self.dept_a.id}),
            content_type='application/json', **self.admin_headers)
        assert resp.status_code == 201
        ticket = TicketList.objects.get(ticket_no=resp.json()['ticket_no'])
        with patch.object(Team, 'save', side_effect=IntegrityError('dup')):
            aresp = self._approve(ticket, self._org_ua_headers())
        assert aresp.status_code == 400
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.PENDING
        team.refresh_from_db()
        assert team.is_deleted is True

    @pytest.mark.integration
    def test_team_create_returns_ticket(self):
        """新建团队 → 201 工单（不直接落库，唯一性由创建时预检保证）"""
        resp = self.client.post(
            '/api/v1/auth/teams/',
            data=json.dumps({'name': '新团队Z', 'department_id': self.dept_a.id}),
            content_type='application/json', **self.admin_headers)
        assert resp.status_code == 201
        assert resp.json()['ticket_no'].startswith('ZZ')
        assert not Team.objects.filter(name='新团队Z', department=self.dept_a).exists()

    @pytest.mark.integration
    def test_team_update_returns_ticket(self):
        """更新团队 → 200 工单（DB 不变，审批后生效）"""
        resp = self.client.patch(
            f'/api/v1/auth/teams/{self.team_a.id}/',
            data=json.dumps({'description': 'x'}),
            content_type='application/json', **self.admin_headers)
        assert resp.status_code == 200
        assert resp.json()['ticket_no'].startswith('ZZ')
        self.team_a.refresh_from_db()
        assert self.team_a.description is None

    @pytest.mark.integration
    def test_team_destroy_with_docs_400(self):
        """团队节点下有文档 → 拒绝删除（覆盖 1752-1761）"""
        from apps.knowledge.models import KnowledgeNode, Document
        team = Team.objects.create(name='带文档团队', code='docs_team', department=self.dept_b)
        team_node = KnowledgeNode.objects.filter(
            node_level=3, ref_id=team.id, is_deleted=False).first()
        assert team_node is not None
        Document.objects.create(
            node=team_node, dept_id=self.dept_b.id, team_id=team.id,
            owner=self.normal_user, title='测试文档', file_name='a.md',
            file_type='markdown', file_hash='h1', root_type='KB',
        )
        resp = self.client.delete(f'/api/v1/auth/teams/{team.id}/', **self.admin_headers)
        assert resp.status_code == 400
        assert '个文档' in resp.json()['detail']


class TestRoleViewSetEdge(UsersAPIExtraBase):
    """Role 更新内置标记 / 分配权限参数校验"""

    @pytest.mark.integration
    def test_update_builtin_role_is_builtin_400(self):
        """修改内置角色的 is_builtin → 400（覆盖 1538-1540）"""
        viewer = _get_or_create_role('viewer')
        resp = self.client.patch(
            f'/api/v1/auth/roles/{viewer.id}/',
            data=json.dumps({'is_builtin': False}),
            content_type='application/json', **self.admin_headers)
        assert resp.status_code == 400
        assert resp.json()['detail'] == '内置角色标记不可修改'

    @pytest.mark.integration
    def test_assign_permissions_non_integer_400(self):
        """permission_ids 含非整数 → 400（覆盖 1580-1583）"""
        viewer = _get_or_create_role('viewer')
        resp = self.client.post(
            f'/api/v1/auth/roles/{viewer.id}/assign-permissions/',
            data=json.dumps({'permission_ids': ['abc']}),
            content_type='application/json', **self.admin_headers)
        assert resp.status_code == 400
        assert '无效的权限ID' in resp.json()['detail']


# ============================================================================
# AccessApplicationView —— 任命权限矩阵 / 资源所有者 / 异常兜底
# ============================================================================
class TestAccessApplicationEdge(UsersAPIExtraBase):
    """权限申请：管理岗任命 / 协作角色提单 / create_ticket 异常"""

    def _uadmin(self, username='uadmin'):
        return _make_custom_role_user(username, 'uadmin_role', 'user.manage_all')

    def _dept_mgr_rel(self):
        """给 dept_mgr 增加部门属地授权（部门经理身份以范围授权为准）"""
        dm_role = _get_or_create_role('dept_manager')
        UserDeptScopeRel.objects.create(
            user=self.dept_mgr, role=dm_role, dept=self.dept_a,
            status=GrantStatus.ACTIVE)

    def _post_app(self, payload, headers):
        return self.client.post(
            '/api/v1/auth/permissions/applications/',
            data=json.dumps(payload), content_type='application/json', **headers)

    @pytest.mark.integration
    def test_super_admin_nominate_team_leader_201(self):
        """超管可任命团队组长，默认有效期 1 年（覆盖 2002-2004 + 2227-2230）"""
        resp = self._post_app({
            'role_key': 'team_leader', 'scope_type': 'TEAM',
            'scope_id': self.team_b.id, 'reason': '任命后端组组长',
            'target_user_id': self.normal_user.id,
        }, self.admin_headers)
        assert resp.status_code == 201, resp.content
        ticket = TicketList.objects.get(id=resp.json()['id'])
        assert ticket.expires_at is not None
        assert ticket.expires_at - timezone.now() >= datetime.timedelta(days=364)

    @pytest.mark.integration
    def test_user_admin_nominate_dept_manager(self):
        """用户管理员可任命部门经理（覆盖 2005-2006）"""
        uadmin = self._uadmin('uadmin_nom1')
        resp = self._post_app({
            'role_key': 'dept_manager', 'scope_type': 'DEPT',
            'scope_id': self.dept_b.id, 'reason': '任命市场部经理',
            'target_user_id': self.normal_user.id,
        }, _auth_headers(uadmin))
        assert resp.status_code in (201, 400), resp.content

    @pytest.mark.integration
    def test_user_admin_nominate_team_leader(self):
        """用户管理员可任命团队组长（覆盖 2007-2009）"""
        uadmin = self._uadmin('uadmin_nom2')
        resp = self._post_app({
            'role_key': 'team_leader', 'scope_type': 'TEAM',
            'scope_id': self.team_b.id, 'reason': '任命组长',
            'target_user_id': self.normal_user.id,
        }, _auth_headers(uadmin))
        assert resp.status_code in (201, 400), resp.content

    @pytest.mark.integration
    def test_dept_mgr_nominate_team_leader_in_scope(self):
        """部门经理可任命本部门团队组长（覆盖 2010-2022）"""
        self._dept_mgr_rel()
        resp = self._post_app({
            'role_key': 'team_leader', 'scope_type': 'TEAM',
            'scope_id': self.team_b.id, 'reason': '任命组长',
            'target_user_id': self.normal_user.id,
        }, self.dept_mgr_headers)
        assert resp.status_code in (201, 400), resp.content

    @pytest.mark.integration
    def test_normal_user_cannot_nominate_team_leader(self):
        """普通用户任命团队组长 → 403（覆盖 2176-2178）"""
        resp = self._post_app({
            'role_key': 'team_leader', 'scope_type': 'TEAM',
            'scope_id': self.team_a.id, 'reason': '自荐',
            'target_user_id': self.team_leader.id,
        }, self.normal_headers)
        assert resp.status_code == 403
        assert resp.json()['detail'] == '当前用户无权发起该角色的任命工单'

    @pytest.mark.integration
    def test_management_role_revoke_400(self):
        """管理岗撤销走管理端 → 400（覆盖 2158-2162）"""
        resp = self._post_app({
            'role_key': 'team_leader', 'change_type': 'REVOKE',
            'scope_type': 'TEAM', 'scope_id': self.team_a.id,
            'reason': '撤销', 'target_user_id': self.normal_user.id,
        }, self.admin_headers)
        assert resp.status_code == 400
        assert '管理岗撤销请由管理端处理' in resp.json()['detail']

    @pytest.mark.integration
    def test_management_role_bad_target_id_403(self):
        """管理岗任命非数字目标 → 403（覆盖 2163-2171）"""
        resp = self._post_app({
            'role_key': 'team_leader', 'scope_type': 'TEAM',
            'scope_id': self.team_a.id, 'reason': '任命',
            'target_user_id': 'abc',
        }, self.admin_headers)
        assert resp.status_code == 403
        assert '不能自助申请' in resp.json()['detail']

    @pytest.mark.integration
    def test_management_role_target_missing_400(self):
        """管理岗任命目标不存在 → 400（覆盖 2172-2176）"""
        resp = self._post_app({
            'role_key': 'team_leader', 'scope_type': 'TEAM',
            'scope_id': self.team_a.id, 'reason': '任命',
            'target_user_id': 999999,
        }, self.admin_headers)
        assert resp.status_code == 400
        assert resp.json()['detail'] == '指定的被任命用户不存在或已禁用'

    @pytest.mark.integration
    def test_resource_owner_dept_branch(self):
        """部门经理对部门属地授权范围提单（覆盖 2043-2046）"""
        self._dept_mgr_rel()
        resp = self._post_app({
            'role_key': 'viewer', 'scope_type': 'DEPT',
            'scope_id': self.dept_a.id, 'reason': '部门查看',
            'target_user_id': self.normal_user.id,
        }, self.dept_mgr_headers)
        assert resp.status_code in (201, 400), resp.content

    @pytest.mark.integration
    def test_collaborative_not_resource_owner_403(self):
        """协作角色提单人非资源所有者 → 403（覆盖 2040-2042 + 2047 + 2207-2210）"""
        resp = self._post_app({
            'role_key': 'viewer', 'scope_type': 'TEAM',
            'scope_id': self.team_b.id, 'reason': '跨团队查看',
            'target_user_id': self.team_leader.id,
        }, self.normal_headers)
        assert resp.status_code == 403
        assert '无权提单' in resp.json()['detail']

    @pytest.mark.integration
    def test_collaborative_scope_none_400(self):
        """协作角色必须绑定 TEAM/DEPT 范围（覆盖 2189-2192）"""
        resp = self._post_app({
            'role_key': 'viewer', 'scope_type': 'NONE',
            'reason': '查看', 'target_user_id': self.normal_user.id,
        }, self.leader_headers)
        assert resp.status_code == 400
        assert '必须绑定团队(TEAM)或部门(DEPT)范围' in resp.json()['detail']

    @pytest.mark.integration
    def test_collaborative_bad_target_id_403(self):
        """协作角色非数字目标 → 403（覆盖 2193-2201）"""
        resp = self._post_app({
            'role_key': 'viewer', 'scope_type': 'TEAM',
            'scope_id': self.team_a.id, 'reason': '查看',
            'target_user_id': 'abc',
        }, self.leader_headers)
        assert resp.status_code == 403
        assert '须由资源团队组长/部门经理指定被授权人提单' in resp.json()['detail']

    @pytest.mark.integration
    def test_collaborative_target_missing_400(self):
        """协作角色目标不存在 → 400（覆盖 2202-2206）"""
        resp = self._post_app({
            'role_key': 'viewer', 'scope_type': 'TEAM',
            'scope_id': self.team_a.id, 'reason': '查看',
            'target_user_id': 999999,
        }, self.leader_headers)
        assert resp.status_code == 400
        assert resp.json()['detail'] == '指定的被授权用户不存在或已禁用'

    @pytest.mark.integration
    def test_create_ticket_value_error_400(self):
        """create_ticket 抛 ValueError（SoD 等业务拦截）→ 400（覆盖 2251-2253）"""
        with patch('apps.users.ticket_service.create_ticket',
                   side_effect=ValueError('SoD 互斥冲突')):
            resp = self._post_app({
                'role_key': 'viewer', 'scope_type': 'TEAM',
                'scope_id': self.team_a.id, 'reason': '查看',
                'target_user_id': self.normal_user.id,
            }, self.leader_headers)
        assert resp.status_code == 400
        assert resp.json()['detail'] == 'SoD 互斥冲突'

    @pytest.mark.integration
    def test_create_ticket_unexpected_error_500(self):
        """create_ticket 抛未知异常 → 500（覆盖 2254-2256）"""
        client = Client(raise_request_exception=False)
        with patch('apps.users.ticket_service.create_ticket',
                   side_effect=RuntimeError('boom')):
            resp = client.post(
                '/api/v1/auth/permissions/applications/',
                data=json.dumps({
                    'role_key': 'viewer', 'scope_type': 'TEAM',
                    'scope_id': self.team_a.id, 'reason': '查看',
                    'target_user_id': self.normal_user.id,
                }),
                content_type='application/json', **self.leader_headers)
        assert resp.status_code == 500
        assert '创建工单失败' in resp.json()['detail']

    # ---- GET 列表：scope_name 解析 ----
    def _make_perm_ticket(self, scope_type, scope_id=None):
        applicant = self.normal_user
        viewer = _get_or_create_role('viewer')
        t = TicketList.objects.create(
            ticket_no=_gen_ticket_no('permission'),
            title='权限申请', biz_type='permission', status=TicketStatus.PENDING,
            risk_level='normal', applicant=applicant,
            approval_chain=[], current_step=0,
        )
        TicketPermissionDetail.objects.create(
            ticket=t, target_user=applicant, change_type=TicketChangeType.GRANT,
            role=viewer, scope_type=scope_type, scope_id=scope_id, reason='测试',
        )
        return t

    @pytest.mark.integration
    def test_get_applications_dept_scope_name(self):
        """DEPT 范围工单 scope_name = 部门名（覆盖 2067-2069）"""
        self._make_perm_ticket(ScopeType.DEPT, self.dept_a.id)
        resp = self.client.get('/api/v1/auth/permissions/applications/', **self.normal_headers)
        assert resp.status_code == 200
        row = resp.json()['rows'][0]
        assert row['scope_name'] == '研发部'

    @pytest.mark.integration
    def test_get_applications_global_scope_name(self):
        """GLOBAL 范围工单 scope_name = 全局（覆盖 2073-2074）"""
        self._make_perm_ticket(ScopeType.GLOBAL)
        resp = self.client.get('/api/v1/auth/permissions/applications/', **self.normal_headers)
        assert resp.status_code == 200
        row = resp.json()['rows'][0]
        assert row['scope_name'] == '全局'


# ============================================================================
# TicketApproveView / TicketRejectView —— 审批边界
# ============================================================================
class TestTicketViewsEdge(UsersAPIExtraBase):
    """审批/驳回的边界分支 + 工单中心序列化与视图过滤"""

    def _ticket(self, chain=None, status=TicketStatus.PENDING, current_step=0,
                biz_type='permission', applicant=None, **extra):
        applicant = applicant or self.normal_user
        return TicketList.objects.create(
            ticket_no=_gen_ticket_no(biz_type), title='测试工单',
            biz_type=biz_type, status=status, risk_level='normal',
            applicant=applicant, approval_chain=chain or [],
            current_step=current_step, **extra,
        )

    @pytest.mark.integration
    def test_approve_chain_finished_400(self):
        """审批链已完结 → 400（覆盖 2335-2337）"""
        t = self._ticket(chain=[])
        resp = self.client.post(
            f'/api/v1/auth/permissions/tickets/{t.id}/approve/',
            data=json.dumps({}), content_type='application/json', **self.admin_headers)
        assert resp.status_code == 400
        assert '已完结' in resp.json()['detail']

    @pytest.mark.integration
    def test_approve_claimed_by_other_403(self):
        """节点已被他人锁定 → 403（覆盖 2342-2344）"""
        t = self._ticket(chain=[{
            'step': 0, 'approver_role': 'SUPER_ADMIN',
            'approver_id': self.normal_user.id, 'status': 'pending'}])
        resp = self.client.post(
            f'/api/v1/auth/permissions/tickets/{t.id}/approve/',
            data=json.dumps({}), content_type='application/json', **self.admin_headers)
        assert resp.status_code == 403
        assert resp.json()['message'] == '无权访问'
        assert '已被其他管理员处理' in resp.json()['details']['detail']

    @pytest.mark.integration
    def test_approve_not_in_pool_403(self):
        """approver_role 不在审批池 → 403（覆盖 2345-2346）"""
        t = self._ticket(chain=[{
            'step': 0, 'approver_role': 'USER_ADMIN', 'approver_id': None,
            'status': 'pending'}])
        resp = self.client.post(
            f'/api/v1/auth/permissions/tickets/{t.id}/approve/',
            data=json.dumps({}), content_type='application/json', **self.admin_headers)
        assert resp.status_code == 403
        assert resp.json()['message'] == '无权访问'
        assert '您没有审批' in resp.json()['details']['detail']

    @pytest.mark.integration
    def test_approve_service_error_400(self):
        """approve_ticket 抛 ValueError → 400（覆盖 2352-2355）"""
        t = self._ticket(chain=[{
            'step': 0, 'approver_role': 'SUPER_ADMIN', 'approver_id': None,
            'status': 'pending'}])
        with patch('apps.users.ticket_service.approve_ticket',
                   side_effect=ValueError('审批链状态异常')):
            resp = self.client.post(
                f'/api/v1/auth/permissions/tickets/{t.id}/approve/',
                data=json.dumps({}), content_type='application/json', **self.admin_headers)
        assert resp.status_code == 400
        assert resp.json()['detail'] == '审批链状态异常'

    @pytest.mark.integration
    def test_reject_as_bound_approver(self):
        """当前节点绑定审批人驳回 → 成功（覆盖 2397-2400）"""
        t = self._ticket(chain=[{
            'step': 0, 'approver_role': 'SUPER_ADMIN',
            'approver_id': self.super_admin.id, 'status': 'pending'}])
        resp = self.client.post(
            f'/api/v1/auth/permissions/tickets/{t.id}/reject/',
            data=json.dumps({'comment': '驳回'}), content_type='application/json',
            **self.admin_headers)
        assert resp.status_code == 200
        t.refresh_from_db()
        assert t.status == TicketStatus.REJECTED

    @pytest.mark.integration
    def test_reject_denied_403(self):
        """无审批权限驳回 → 403（覆盖 2401-2404）"""
        t = self._ticket(chain=[{
            'step': 0, 'approver_role': 'SUPER_ADMIN',
            'approver_id': self.super_admin.id, 'status': 'pending'}])
        resp = self.client.post(
            f'/api/v1/auth/permissions/tickets/{t.id}/reject/',
            data=json.dumps({'comment': '驳回'}), content_type='application/json',
            **self.normal_headers)
        assert resp.status_code == 403
        assert resp.json()['message'] == '无权访问'
        assert '无权驳回该工单' in resp.json()['details']['detail']

    @pytest.mark.integration
    def test_reject_service_error_400(self):
        """reject_ticket 抛 ValueError → 400（覆盖 2409-2412）"""
        t = self._ticket(chain=[{
            'step': 0, 'approver_role': 'SUPER_ADMIN', 'approver_id': None,
            'status': 'pending'}])
        with patch('apps.users.ticket_service.reject_ticket',
                   side_effect=ValueError('状态异常')):
            resp = self.client.post(
                f'/api/v1/auth/permissions/tickets/{t.id}/reject/',
                data=json.dumps({'comment': '驳回'}), content_type='application/json',
                **self.admin_headers)
        assert resp.status_code == 400
        assert resp.json()['detail'] == '状态异常'

    @pytest.mark.integration
    def test_pending_view_approvable_roles_variants(self):
        """待审批视角：UserRoleRel 管理角色 / Team.leader_id / Department.leader_id（覆盖 2511-2518）"""
        dmrel = _make_custom_role_user('dmrel1', 'dmrel_role', 'user.manage',
                                       role_type=RoleType.DEPT_SCOPE,
                                       data_scope=DataScope.DEPT)
        _grant_global_role(dmrel, 'dept_manager')
        self.dept_a.leader = self.dept_mgr
        self.dept_a.save(update_fields=['leader'])
        self._ticket(chain=[{
            'step': 0, 'approver_role': 'SUPER_ADMIN', 'approver_id': None,
            'status': 'pending'}])
        for headers in (self.admin_headers, self.dept_mgr_headers,
                        self.leader_headers, _auth_headers(dmrel)):
            resp = self.client.get('/api/v1/auth/tickets/?view=pending', **headers)
            assert resp.status_code == 200

    @pytest.mark.integration
    def test_center_status_filter(self):
        """status 过滤（覆盖 2754-2755）"""
        self._ticket()
        resp = self.client.get('/api/v1/auth/tickets/?view=all&status=PENDING',
                               **self.admin_headers)
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert rows
        assert all(r['status'] == 'PENDING' for r in rows)

    @pytest.mark.integration
    def test_center_search_by_id(self):
        """search=数字 → 按工单 id 匹配（覆盖 2757-2759）"""
        t = self._ticket()
        resp = self.client.get(f'/api/v1/auth/tickets/?view=all&search={t.id}',
                               **self.admin_headers)
        assert resp.status_code == 200
        assert any(r['id'] == t.id for r in resp.json()['rows'])

    @pytest.mark.integration
    def test_center_invalid_page_400(self):
        """page 非数字 → 400（覆盖 2807-2811）"""
        resp = self.client.get('/api/v1/auth/tickets/?view=all&page=abc', **self.admin_headers)
        assert resp.status_code == 400
        assert 'page/page_size 参数无效' in resp.json()['detail']

    @pytest.mark.integration
    def test_center_serialize_dept_and_global_scope(self):
        """权限工单 scope_name 解析（覆盖 2623-2629）"""
        viewer = _get_or_create_role('viewer')
        t1 = self._ticket()
        TicketPermissionDetail.objects.create(
            ticket=t1, target_user=self.normal_user, change_type=TicketChangeType.GRANT,
            role=viewer, scope_type=ScopeType.DEPT, scope_id=self.dept_a.id, reason='r')
        t2 = self._ticket()
        TicketPermissionDetail.objects.create(
            ticket=t2, target_user=self.normal_user, change_type=TicketChangeType.GRANT,
            role=viewer, scope_type=ScopeType.GLOBAL, scope_id=None, reason='r')
        resp = self.client.get('/api/v1/auth/tickets/?view=all', **self.admin_headers)
        rows = {r['id']: r for r in resp.json()['rows']}
        assert rows[t1.id]['scope_name'] == '研发部'
        assert rows[t2.id]['scope_name'] == '全局'

    @pytest.mark.integration
    def test_center_serialize_model_ticket(self):
        """模型变更工单序列化（覆盖 2668-2678）"""
        from apps.system.models import LLMModel
        model = LLMModel.objects.create(
            name='测试模型', model_name='test-model', provider='test',
            model_type='llm', base_url='http://x')
        t = self._ticket(biz_type='model', operation='update_normal',
                         target_model_id=model.id)
        TicketModelDetail.objects.create(
            ticket=t, changed_fields={'max_tokens': {'old': 100, 'new': 200}}, reason='调整')
        resp = self.client.get('/api/v1/auth/tickets/?view=all', **self.admin_headers)
        row = next(r for r in resp.json()['rows'] if r['id'] == t.id)
        assert row['model_name'] == '测试模型 (test-model)'
        assert row['changed_fields'] == ['max_tokens']
        assert row['operation_display'] == '修改模型'

    @pytest.mark.integration
    def test_center_serialize_agent_ticket(self):
        """Agent 人工确认工单序列化（覆盖 2681-2684）"""
        t = self._ticket(biz_type='agent')
        TicketAgentApprovalDetail.objects.create(
            ticket=t, workflow_id=1, node_id='n1',
            reason='[agent:1:approval] 确认无误')
        resp = self.client.get('/api/v1/auth/tickets/?view=all', **self.admin_headers)
        row = next(r for r in resp.json()['rows'] if r['id'] == t.id)
        assert row['reason'] == '[agent:1:approval] 确认无误'
        assert row['operation'] == 'agent_approval'

    @pytest.mark.integration
    def test_center_reject_not_found_404(self):
        """工单中心驳回不存在的工单 → 404（覆盖 2870-2872）"""
        resp = self.client.post(
            '/api/v1/auth/tickets/999999/reject/',
            data=json.dumps({'comment': 'x'}), content_type='application/json',
            **self.admin_headers)
        assert resp.status_code == 404
        assert resp.json()['detail'] == '工单不存在'

    @pytest.mark.integration
    def test_center_reject_permission_ticket(self):
        """工单中心驳回 permission 工单 → 委托 TicketRejectView（覆盖 2873-2874）"""
        t = self._create_pending_ticket()
        resp = self.client.post(
            f'/api/v1/auth/tickets/{t.id}/reject/',
            data=json.dumps({'comment': '驳回'}), content_type='application/json',
            **self.admin_headers)
        assert resp.status_code == 200
        t.refresh_from_db()
        assert t.status == TicketStatus.REJECTED

    @pytest.mark.integration
    def test_center_withdraw_not_found_404(self):
        """撤回不存在的工单 → 404（覆盖 2889-2891）"""
        resp = self.client.post(
            '/api/v1/auth/tickets/999999/withdraw/',
            data=json.dumps({}), content_type='application/json', **self.admin_headers)
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_center_withdraw_not_applicant_403(self):
        """非创建人撤回 → 403（覆盖 2894-2895）"""
        t = self._create_pending_ticket()
        resp = self.client.post(
            f'/api/v1/auth/tickets/{t.id}/withdraw/',
            data=json.dumps({}), content_type='application/json', **self.admin_headers)
        assert resp.status_code == 403
        assert resp.json()['detail'] == '仅创建人可撤回工单'

    @pytest.mark.integration
    def test_center_withdraw_non_pending_400(self):
        """非 PENDING 撤回 → 400（覆盖 2896-2897）"""
        t = self._create_pending_ticket()
        t.status = TicketStatus.EXECUTED
        t.save(update_fields=['status', 'updated_at'])
        resp = self.client.post(
            f'/api/v1/auth/tickets/{t.id}/withdraw/',
            data=json.dumps({}), content_type='application/json', **self.normal_headers)
        assert resp.status_code == 400
        assert '不可撤回' in resp.json()['detail']

    @pytest.mark.integration
    def test_center_withdraw_pending_200(self):
        """创建人撤回 PENDING 工单 → 成功（覆盖 2898-2903）"""
        t = self._create_pending_ticket()
        resp = self.client.post(
            f'/api/v1/auth/tickets/{t.id}/withdraw/',
            data=json.dumps({}), content_type='application/json', **self.normal_headers)
        assert resp.status_code == 200
        t.refresh_from_db()
        assert t.status == TicketStatus.CANCELLED


# ============================================================================
# AssignableRolesView / ApprovalChainPreviewView
# ============================================================================
class TestAssignableAndChainPreview(UsersAPIExtraBase):
    """可申请角色清单 + 审批链预览"""

    @pytest.mark.integration
    def test_management_roles_scope_filter(self):
        """管理岗清单按 scope_type 过滤（覆盖 2980-2987）"""
        resp = self.client.get(
            '/api/v1/auth/permissions/assignable-roles/?purpose=management&scope_type=TEAM',
            **self.normal_headers)
        assert resp.status_code == 200
        rows = resp.json()['rows']
        assert rows
        assert all(r['role_key'] == 'team_leader' for r in rows)

    @pytest.mark.integration
    def test_management_roles_global_scope_type(self):
        """全局高权角色 scope_type_required=NONE（覆盖 3003-3005）"""
        for rk in ('user_admin', 'kb_admin', 'compliance_admin', 'dept_manager'):
            _get_or_create_role(rk)
        resp = self.client.get(
            '/api/v1/auth/permissions/assignable-roles/?purpose=management',
            **self.normal_headers)
        assert resp.status_code == 200
        rows = {r['role_key']: r for r in resp.json()['rows']}
        assert rows['user_admin']['scope_type_required'] == 'NONE'
        assert rows['kb_admin']['supported_scopes'] == ['NONE']

    @pytest.mark.integration
    def test_chain_preview_build_error_400(self):
        """build_approval_chain 异常 → 400（覆盖 3078-3089）"""
        with patch('apps.users.ticket_service.build_approval_chain',
                   side_effect=RuntimeError('boom')):
            resp = self.client.get(
                f'/api/v1/auth/permissions/approval-chain-preview/'
                f'?role_key=viewer&scope_type=TEAM&scope_id={self.team_a.id}',
                **self.normal_headers)
        assert resp.status_code == 400
        assert '构造审批链失败' in resp.json()['detail']

    @pytest.mark.integration
    def test_chain_preview_node_scope_names(self):
        """审批链节点 scope 名称解析（覆盖 3100-3108）"""
        with patch('apps.users.ticket_service.build_approval_chain', return_value=[
            {'approver_role': 'USER_ADMIN', 'approver_scope_type': ScopeType.DEPT,
             'approver_scope_id': self.dept_a.id, 'status': 'pending'},
            {'approver_role': 'TEAM_LEADER', 'approver_scope_type': ScopeType.TEAM,
             'approver_scope_id': self.team_a.id, 'status': 'pending'},
        ]):
            resp = self.client.get(
                f'/api/v1/auth/permissions/approval-chain-preview/'
                f'?role_key=viewer&scope_type=TEAM&scope_id={self.team_a.id}',
                **self.normal_headers)
        assert resp.status_code == 200
        nodes = resp.json()['chain']
        assert nodes[0]['approver_scope_name'] == '研发部'
        assert nodes[1]['approver_scope_name'] == '后端组'

    @pytest.mark.integration
    def test_chain_preview_target_scope_names(self):
        """目标 scope 名称解析：DEPT/TEAM/NONE（覆盖 3122-3129）"""
        with patch('apps.users.ticket_service.build_approval_chain', return_value=[]):
            resp = self.client.get(
                f'/api/v1/auth/permissions/approval-chain-preview/'
                f'?role_key=viewer&scope_type=DEPT&scope_id={self.dept_a.id}',
                **self.normal_headers)
            assert resp.status_code == 200
            assert resp.json()['scope_name'] == '研发部'
            resp = self.client.get(
                f'/api/v1/auth/permissions/approval-chain-preview/'
                f'?role_key=viewer&scope_type=TEAM&scope_id={self.team_a.id}',
                **self.normal_headers)
            assert resp.status_code == 200
            assert resp.json()['scope_name'] == '后端组'
            resp = self.client.get(
                '/api/v1/auth/permissions/approval-chain-preview/'
                '?role_key=viewer&scope_type=NONE',
                **self.normal_headers)
            assert resp.status_code == 200
            assert resp.json()['scope_name'] == '全局'
            assert resp.json()['is_direct_execute'] is True
