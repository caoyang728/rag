"""
apps.users.views 组织架构补充测试 —— 部门 / 团队 CRUD 与权限拦截

与 test_views.py 互补：DepartmentViewSet / TeamViewSet 全套 CRUD 与权限校验。
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


class TestDepartmentViewSet(UsersAPIExtraBase):
    """部门增删改查：管理权限 / 自动编码 / 软删恢复 / 删除约束"""

    @pytest.mark.integration
    def test_list_authenticated_200(self):
        """任何登录用户可查看部门列表（IsAuthenticated，含 user_count/teams 聚合）"""
        resp = self.client.get('/api/v1/auth/departments/', **self.normal_headers)
        assert resp.status_code == 200
        results = resp.json()['results']
        assert any(d['name'] == '研发部' for d in results)
        rd = next(d for d in results if d['name'] == '研发部')
        assert rd['user_count'] >= 1
        assert any(t['name'] == '后端组' for t in rd['teams'])

    @pytest.mark.integration
    def test_list_anonymous_401(self):
        """匿名查看部门列表 → 401"""
        resp = self.client.get('/api/v1/auth/departments/', **self.anon_headers)
        assert resp.status_code in (401, 403)

    @pytest.mark.integration
    def test_create_by_admin_201_with_auto_code(self):
        """超管创建部门 → 201，未传 code 时自动生成拼音首字母编码

        注意:部门名避开公共基座已建的"测试部"(UsersAPITestBase),防同名唯一冲突。
        """
        resp = self.client.post(
            '/api/v1/auth/departments/',
            data=json.dumps({'name': '测试部X'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data['name'] == '测试部X'
        assert data['code']  # 自动编码非空
        assert Department.objects.filter(name='测试部X', is_deleted=False).exists()

    @pytest.mark.integration
    def test_create_normal_user_403(self):
        """普通用户创建部门 → 403（_check_can_manage_dept 基于 user.manage_all）"""
        resp = self.client.post(
            '/api/v1/auth/departments/',
            data=json.dumps({'name': '新部门'}),
            content_type='application/json', **self.normal_headers,
        )
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_create_duplicate_name_400(self):
        """同名部门重复创建 → 400"""
        resp = self.client.post(
            '/api/v1/auth/departments/',
            data=json.dumps({'name': '研发部'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400
        assert '已存在' in resp.json().get('detail', '')

    @pytest.mark.integration
    def test_create_with_leader_id_ignored_201(self):
        """create 传 leader_id → 201 但 leader 不落库

        部门经理改由"任命工单"(GRANT dept_manager)设置,审批通过后同步 leader_id,
        组织 CRUD 不再直接写 leader_id。
        """
        resp = self.client.post(
            '/api/v1/auth/departments/',
            data=json.dumps({'name': '新部门X', 'leader_id': 999999}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()['leader_id'] is None

    @pytest.mark.integration
    def test_create_restores_deleted_dept(self):
        """同名部门曾软删 → 直接恢复（201），不重复创建"""
        dept = Department.objects.create(name='旧部门', code='old')
        dept.is_deleted = True
        dept.save()
        resp = self.client.post(
            '/api/v1/auth/departments/',
            data=json.dumps({'name': '旧部门'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 201
        dept.refresh_from_db()
        assert dept.is_deleted is False

    @pytest.mark.integration
    def test_update_department_200(self):
        """超管更新部门名称 → 200"""
        resp = self.client.patch(
            f'/api/v1/auth/departments/{self.dept_a.id}/',
            data=json.dumps({'name': '研发一部'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        self.dept_a.refresh_from_db()
        assert self.dept_a.name == '研发一部'

    @pytest.mark.integration
    def test_update_leader_id_ignored(self):
        """PATCH leader_id → 200 但 leader 不落库(任命工单设置,CRUD 不再直接写)"""
        resp = self.client.patch(
            f'/api/v1/auth/departments/{self.dept_a.id}/',
            data=json.dumps({'leader_id': self.normal_user.id}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        self.dept_a.refresh_from_db()
        assert self.dept_a.leader_id is None

    @pytest.mark.integration
    def test_update_invalid_leader_ignored_200(self):
        """leader_id 指向不存在用户 → 200 且被忽略(不再走 _set_leader 校验)"""
        resp = self.client.patch(
            f'/api/v1/auth/departments/{self.dept_a.id}/',
            data=json.dumps({'leader_id': 999999}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200

    @pytest.mark.integration
    def test_destroy_with_users_400(self):
        """部门下仍有用户 → 400 无法删除"""
        resp = self.client.delete(
            f'/api/v1/auth/departments/{self.dept_a.id}/', **self.admin_headers)
        assert resp.status_code == 400
        assert '用户' in resp.json().get('detail', '')

    @pytest.mark.integration
    def test_destroy_with_teams_400(self):
        """部门下仍有团队 → 400 无法删除（需先删除/迁移团队）"""
        empty_dept = Department.objects.create(name='空部门', code='empty')
        Team.objects.create(name='遗留团队', code='legacy', department=empty_dept)
        resp = self.client.delete(
            f'/api/v1/auth/departments/{empty_dept.id}/', **self.admin_headers)
        assert resp.status_code == 400
        assert '团队' in resp.json().get('detail', '')

    @pytest.mark.integration
    def test_destroy_empty_dept_204(self):
        """部门下无用户无团队 → 软删成功 204"""
        empty_dept = Department.objects.create(name='空部门2', code='empty2')
        resp = self.client.delete(
            f'/api/v1/auth/departments/{empty_dept.id}/', **self.admin_headers)
        assert resp.status_code == 204
        empty_dept.refresh_from_db()
        assert empty_dept.is_deleted is True

    @pytest.mark.integration
    def test_destroy_normal_user_403(self):
        """普通用户删除部门 → 403"""
        resp = self.client.delete(
            f'/api/v1/auth/departments/{self.dept_b.id}/', **self.normal_headers)
        assert resp.status_code == 403


# ============================================================================
# TeamViewSet —— 团队 CRUD
# ============================================================================

class TestTeamViewSet(UsersAPIExtraBase):
    """团队增删改查：部门绑定 / 自动编码 / 同部门重名 / 成员删除约束"""

    @pytest.mark.integration
    def test_list_with_department_filter(self):
        """?department_id= 过滤团队（权限申请部门→团队级联选择用）"""
        resp = self.client.get(
            f'/api/v1/auth/teams/?department_id={self.dept_a.id}', **self.normal_headers)
        assert resp.status_code == 200
        results = resp.json()['results']
        assert all(t['department_id'] == self.dept_a.id for t in results)

    @pytest.mark.integration
    def test_create_team_201(self):
        """超管创建团队 → 201（自动编码：部门编码_拼音首字母）"""
        resp = self.client.post(
            '/api/v1/auth/teams/',
            data=json.dumps({'name': '测试组', 'department_id': self.dept_a.id}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data['name'] == '测试组'
        assert data['code'].startswith('rd_')
        assert Team.objects.filter(name='测试组', department=self.dept_a).exists()

    @pytest.mark.integration
    def test_create_missing_department_400(self):
        """缺 department_id → 400（部门ID不能为空）"""
        resp = self.client.post(
            '/api/v1/auth/teams/',
            data=json.dumps({'name': '无部门团队'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_nonexistent_department_400(self):
        """department_id 不存在 → 400（指定的部门不存在）"""
        resp = self.client.post(
            '/api/v1/auth/teams/',
            data=json.dumps({'name': '幽灵团队', 'department_id': 999999}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_duplicate_name_in_dept_400(self):
        """同部门下重名团队 → 400"""
        resp = self.client.post(
            '/api/v1/auth/teams/',
            data=json.dumps({'name': '后端组', 'department_id': self.dept_a.id}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400
        assert '已存在团队' in resp.json().get('detail', '')

    @pytest.mark.integration
    def test_create_normal_user_403(self):
        """普通用户创建团队 → 403（_check_can_manage_team）"""
        resp = self.client.post(
            '/api/v1/auth/teams/',
            data=json.dumps({'name': '越权团队', 'department_id': self.dept_a.id}),
            content_type='application/json', **self.normal_headers,
        )
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_create_with_leader_id_ignored(self):
        """创建团队并传 leader_id → 201 但组长不落库(任命工单设置,CRUD 不再直接写)"""
        resp = self.client.post(
            '/api/v1/auth/teams/',
            data=json.dumps({
                'name': '带组长团队', 'department_id': self.dept_a.id,
                'leader_id': self.normal_user.id,
            }),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()['leader_id'] is None

    @pytest.mark.integration
    def test_create_restores_deleted_team(self):
        """同部门同名团队曾软删 → 恢复（201）"""
        team = Team.objects.create(name='旧团队', code='old_team', department=self.dept_a)
        team.is_deleted = True
        team.save()
        resp = self.client.post(
            '/api/v1/auth/teams/',
            data=json.dumps({'name': '旧团队', 'department_id': self.dept_a.id}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 201
        team.refresh_from_db()
        assert team.is_deleted is False

    @pytest.mark.integration
    def test_update_team_200(self):
        """超管更新团队名称 → 200"""
        resp = self.client.patch(
            f'/api/v1/auth/teams/{self.team_a.id}/',
            data=json.dumps({'description': '新描述'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        self.team_a.refresh_from_db()
        assert self.team_a.description == '新描述'

    @pytest.mark.integration
    def test_destroy_with_members_400(self):
        """团队下仍有成员 → 400 无法删除"""
        resp = self.client.delete(f'/api/v1/auth/teams/{self.team_a.id}/', **self.admin_headers)
        assert resp.status_code == 400
        assert '成员' in resp.json().get('detail', '')

    @pytest.mark.integration
    def test_destroy_empty_team_204(self):
        """无成员团队 → 软删成功 204"""
        empty_team = Team.objects.create(name='空团队', code='empty_team', department=self.dept_b)
        resp = self.client.delete(f'/api/v1/auth/teams/{empty_team.id}/', **self.admin_headers)
        assert resp.status_code == 204
        empty_team.refresh_from_db()
        assert empty_team.is_deleted is True

    @pytest.mark.integration
    def test_destroy_normal_user_403(self):
        """普通用户删除团队 → 403"""
        resp = self.client.delete(f'/api/v1/auth/teams/{self.team_b.id}/', **self.normal_headers)
        assert resp.status_code == 403


# ============================================================================
# RoleViewSet —— 角色 CRUD + 权限分配
# ============================================================================

