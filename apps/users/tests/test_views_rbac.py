"""
apps.users.views RBAC 补充测试 —— 角色 / 权限点 / 数据范围 / 审批人

与 test_views.py 互补：RoleViewSet / PermissionViewSet 全套 CRUD 与权限拦截、
MyPermissionsScopeRel、PermissionApproversView（废弃接口）。
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
    PermissionApprovalTicket, TicketStatus, TicketChangeType, ScopeType,
    GrantStatus, RoleType, DataScope,
)
from apps.users.tests.test_views_base import (
    _get_or_create_role, _create_user, _grant_permission, _grant_global_role,
    _auth_headers, FakeRedis, UsersAPIExtraBase,
)


class TestRoleViewSet(UsersAPIExtraBase):
    """角色管理：仅超管可操作；内置角色保护；assign_permissions"""

    @pytest.mark.integration
    def test_list_super_admin_200(self):
        """超管查看角色列表 → 200（含 permission_ids）"""
        resp = self.client.get('/api/v1/auth/roles/', **self.admin_headers)
        assert resp.status_code == 200
        roles = resp.json()
        assert any(r['code'] == 'viewer' for r in roles)

    @pytest.mark.integration
    def test_list_normal_user_403(self):
        """普通用户查看角色列表 → 403（仅超级管理员可操作）"""
        resp = self.client.get('/api/v1/auth/roles/', **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_list_anonymous_401(self):
        """匿名查看角色列表 → 401"""
        resp = self.client.get('/api/v1/auth/roles/', **self.anon_headers)
        assert resp.status_code in (401, 403)

    @pytest.mark.integration
    def test_create_role_201(self):
        """超管创建自定义角色 → 201"""
        resp = self.client.post(
            '/api/v1/auth/roles/',
            data=json.dumps({'code': 'custom_role', 'name': '自定义角色'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 201
        assert Role.objects.filter(role_key='custom_role').exists()

    @pytest.mark.integration
    def test_create_role_invalid_code_400(self):
        """角色编码格式非法（大写/特殊字符）→ 400"""
        resp = self.client.post(
            '/api/v1/auth/roles/',
            data=json.dumps({'code': 'Bad Code!', 'name': '非法角色'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_retrieve_role_200(self):
        """超管查看角色详情 → 200"""
        viewer = _get_or_create_role('viewer')
        resp = self.client.get(f'/api/v1/auth/roles/{viewer.id}/', **self.admin_headers)
        assert resp.status_code == 200
        assert resp.json()['code'] == 'viewer'

    @pytest.mark.integration
    def test_update_builtin_role_code_400(self):
        """修改内置角色编码 → 400（内置角色编码不可修改）"""
        viewer = _get_or_create_role('viewer')
        resp = self.client.patch(
            f'/api/v1/auth/roles/{viewer.id}/',
            data=json.dumps({'code': 'viewer2'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_update_role_with_is_builtin_400(self):
        """通过 API 修改 is_builtin 字段 → 400（字段不可通过 API 修改）"""
        custom = Role.objects.create(role_key='not_builtin_role', name='非内置角色')
        resp = self.client.patch(
            f'/api/v1/auth/roles/{custom.id}/',
            data=json.dumps({'is_builtin': True}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_update_custom_role_200(self):
        """更新自定义角色名称 → 200"""
        custom = Role.objects.create(role_key='custom_upd', name='旧名')
        resp = self.client.patch(
            f'/api/v1/auth/roles/{custom.id}/',
            data=json.dumps({'name': '新名'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['name'] == '新名'

    @pytest.mark.integration
    def test_destroy_builtin_role_400(self):
        """删除内置角色 → 400（内置角色不可删除）"""
        viewer = _get_or_create_role('viewer')
        resp = self.client.delete(f'/api/v1/auth/roles/{viewer.id}/', **self.admin_headers)
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_destroy_role_in_use_400(self):
        """删除仍在被用户使用的角色 → 400"""
        custom = Role.objects.create(role_key='in_use_role', name='在用角色')
        _grant_global_role(self.normal_user, 'in_use_role')
        resp = self.client.delete(f'/api/v1/auth/roles/{custom.id}/', **self.admin_headers)
        assert resp.status_code == 400
        assert '使用' in resp.json().get('detail', '')

    @pytest.mark.integration
    def test_destroy_custom_role_204(self):
        """删除未使用的自定义角色 → 204"""
        custom = Role.objects.create(role_key='free_role', name='空闲角色')
        resp = self.client.delete(f'/api/v1/auth/roles/{custom.id}/', **self.admin_headers)
        assert resp.status_code == 204
        assert not Role.objects.filter(id=custom.id).exists()

    @pytest.mark.integration
    def test_assign_permissions_success(self):
        """批量设置角色权限 → 200，返回更新数量"""
        viewer = _get_or_create_role('viewer')
        perm = Permission.objects.create(
            permission_key='test.perm.read', permission_name='测试权限', module='test')
        resp = self.client.post(
            f'/api/v1/auth/roles/{viewer.id}/assign-permissions/',
            data=json.dumps({'permission_ids': [perm.id]}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        assert '已更新 1 个权限' in resp.json()['detail']
        assert RolePermissionRel.objects.filter(role=viewer, permission=perm, is_active=True).exists()

    @pytest.mark.integration
    def test_assign_permissions_not_a_list_400(self):
        """permission_ids 非数组 → 400"""
        viewer = _get_or_create_role('viewer')
        resp = self.client.post(
            f'/api/v1/auth/roles/{viewer.id}/assign-permissions/',
            data=json.dumps({'permission_ids': 'notalist'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_assign_permissions_invalid_id_400(self):
        """permission_ids 含非正整数 → 400"""
        viewer = _get_or_create_role('viewer')
        resp = self.client.post(
            f'/api/v1/auth/roles/{viewer.id}/assign-permissions/',
            data=json.dumps({'permission_ids': [0, -1, 'abc']}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_assign_permissions_skips_unknown_ids(self):
        """permission_ids 含不存在的权限 → 200 且跳过（skipped 计数）"""
        viewer = _get_or_create_role('viewer')
        resp = self.client.post(
            f'/api/v1/auth/roles/{viewer.id}/assign-permissions/',
            data=json.dumps({'permission_ids': [999999]}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json().get('skipped') == 1

    @pytest.mark.integration
    def test_assign_permissions_normal_user_403(self):
        """普通用户分配角色权限 → 403"""
        viewer = _get_or_create_role('viewer')
        resp = self.client.post(
            f'/api/v1/auth/roles/{viewer.id}/assign-permissions/',
            data=json.dumps({'permission_ids': []}),
            content_type='application/json', **self.normal_headers,
        )
        assert resp.status_code == 403


# ============================================================================
# PermissionViewSet —— 权限点 CRUD
# ============================================================================

class TestPermissionViewSet(UsersAPIExtraBase):
    """权限点管理：仅超管可操作；内置权限点保护；被引用保护"""

    @pytest.mark.integration
    def test_list_super_admin_200(self):
        """超管查看权限点列表 → 200"""
        resp = self.client.get('/api/v1/auth/permissions/', **self.admin_headers)
        assert resp.status_code == 200
        assert 'results' in resp.json()

    @pytest.mark.integration
    def test_list_normal_user_403(self):
        """普通用户查看权限点 → 403（仅超级管理员可操作）"""
        resp = self.client.get('/api/v1/auth/permissions/', **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_list_anonymous_401(self):
        """匿名查看权限点 → 401"""
        resp = self.client.get('/api/v1/auth/permissions/', **self.anon_headers)
        assert resp.status_code in (401, 403)

    @pytest.mark.integration
    def test_create_permission_201(self):
        """超管创建权限点 → 201"""
        resp = self.client.post(
            '/api/v1/auth/permissions/',
            data=json.dumps({'code': 'test.doc.read', 'name': '读文档', 'module': 'test'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 201
        assert Permission.objects.filter(permission_key='test.doc.read').exists()

    @pytest.mark.integration
    def test_retrieve_permission_200(self):
        """超管查看权限点详情 → 200"""
        perm = Permission.objects.create(
            permission_key='test.perm.view', permission_name='查看', module='test')
        resp = self.client.get(f'/api/v1/auth/permissions/{perm.id}/', **self.admin_headers)
        assert resp.status_code == 200
        assert resp.json()['code'] == 'test.perm.view'

    @pytest.mark.integration
    def test_update_builtin_permission_forbidden_403(self):
        """修改内置权限点的核心字段 → 403（内置权限点不允许修改核心字段）"""
        perm = Permission.objects.create(
            permission_key='test.builtin.read', permission_name='内置权限',
            module='test', is_builtin=True)
        resp = self.client.patch(
            f'/api/v1/auth/permissions/{perm.id}/',
            data=json.dumps({'module': 'hacked'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_update_custom_permission_200(self):
        """更新自定义权限点描述 → 200"""
        perm = Permission.objects.create(
            permission_key='test.custom.read', permission_name='自定义', module='test')
        resp = self.client.patch(
            f'/api/v1/auth/permissions/{perm.id}/',
            data=json.dumps({'description': '新描述'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['description'] == '新描述'

    @pytest.mark.integration
    def test_destroy_builtin_permission_403(self):
        """删除内置权限点 → 403（内置系统权限不允许删除）"""
        perm = Permission.objects.create(
            permission_key='test.builtin.del', permission_name='内置',
            module='test', is_builtin=True)
        resp = self.client.delete(f'/api/v1/auth/permissions/{perm.id}/', **self.admin_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_destroy_referenced_permission_400(self):
        """删除被角色引用的权限点 → 400（请先解除角色关联）"""
        perm = Permission.objects.create(
            permission_key='test.referenced.read', permission_name='被引用', module='test')
        role = Role.objects.create(role_key='ref_role', name='引用角色')
        RolePermissionRel.objects.create(role=role, permission=perm, is_active=True)
        resp = self.client.delete(f'/api/v1/auth/permissions/{perm.id}/', **self.admin_headers)
        assert resp.status_code == 400
        assert '引用' in resp.json().get('detail', '')

    @pytest.mark.integration
    def test_destroy_unreferenced_permission_204(self):
        """删除未被引用的权限点 → 204"""
        perm = Permission.objects.create(
            permission_key='test.free.del', permission_name='空闲', module='test')
        resp = self.client.delete(f'/api/v1/auth/permissions/{perm.id}/', **self.admin_headers)
        assert resp.status_code == 204
        assert not Permission.objects.filter(id=perm.id).exists()


# ============================================================================
# MyPermissionsView 补充 —— 团队/部门属地角色的 scope 信息
# ============================================================================

class TestMyPermissionsScopeRel(UsersAPIExtraBase):
    """MyPermissionsView 已覆盖全局角色路径，此处补充属地授权（team/dept scope）分支"""

    @pytest.mark.integration
    def test_scope_rels_included_in_roles(self):
        """持有团队/部门属地角色时，roles 返回对应 scope_type 与 scope_name

        团队属地（UserTeamScopeRel）与部门属地（UserDeptScopeRel）是 MyPermissionsView
        中与全局角色并列的两条分支，未覆盖会导致前端拿不到属地角色信息。
        """
        viewer = _get_or_create_role('viewer')
        UserTeamScopeRel.objects.create(
            user=self.normal_user, role=viewer, team=self.team_b, status=GrantStatus.ACTIVE)
        UserDeptScopeRel.objects.create(
            user=self.normal_user, role=viewer, dept=self.dept_b, status=GrantStatus.ACTIVE)
        resp = self.client.get('/api/v1/auth/permissions/me/', **self.normal_headers)
        assert resp.status_code == 200
        roles = resp.json()['roles']
        scope_types = {r['scope_type'] for r in roles}
        assert 'TEAM' in scope_types and 'DEPT' in scope_types
        team_role = next(r for r in roles if r['scope_type'] == 'TEAM')
        assert team_role['scope_name'] == '前端组'


# ============================================================================
# PermissionApproversView —— 废弃审批人接口（向后兼容）
# ============================================================================

class TestPermissionApproversView(UsersAPIExtraBase):
    """废弃接口保留仅为兼容旧前端：按 scope 返回可选的审批人"""

    @pytest.mark.integration
    def test_team_scope_lists_team_leader(self):
        """scope=team 返回用户所属团队的负责人（normal_user 在团队 A，组长为 leader）"""
        resp = self.client.get('/api/v1/auth/permissions/approvers/?scope=team', **self.normal_headers)
        assert resp.status_code == 200
        ids = {a['id'] for a in resp.json()['approvers']}
        assert self.team_leader.id in ids

    @pytest.mark.integration
    def test_all_scope_includes_super_admin(self):
        """scope=all 额外包含持 super_admin 角色的用户（按角色反查）"""
        _grant_global_role(self.super_admin, 'super_admin')
        resp = self.client.get('/api/v1/auth/permissions/approvers/?scope=all', **self.normal_headers)
        assert resp.status_code == 200
        ids = {a['id'] for a in resp.json()['approvers']}
        assert self.super_admin.id in ids

    @pytest.mark.integration
    def test_anonymous_401(self):
        """匿名访问废弃接口 → 401"""
        resp = self.client.get('/api/v1/auth/permissions/approvers/')
        assert resp.status_code in (401, 403)


# ============================================================================
# AccessApplicationView —— 权限申请（GET 列表 / POST 提交）
# ============================================================================

