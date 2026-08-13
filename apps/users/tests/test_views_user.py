"""
apps.users.views 用户管理补充测试 —— 列表筛选 / 创建更新 / 动作 / 导入导出

与 test_views.py 互补：
- 列表筛选（search / department_id / team_id / role_id / status）、分页
- 部门经理/团队组长数据范围过滤、create 带角色/团队/默认密码、部门经理唯一性校验
- update 角色变更 / 状态变更 / 越权编辑、revive 恢复、permission_detail
- export / batch_export / batch_import / import_template / form_options、assign_roles
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


class TestUserViewSetListFilters(UsersAPIExtraBase):
    """UserViewSet 列表查询：search / 组织筛选 / 状态筛选 / 分页 / 管理范围过滤"""

    def _list(self, url, **headers):
        return self.client.get(url, **headers)

    @pytest.mark.integration
    def test_list_filter_by_search(self):
        """?search= 应按 username/email/real_name 模糊匹配（get_queryset 搜索分支）"""
        _create_user(username='zhangwei', email='zw@test.com', real_name='张伟')
        resp = self._list('/api/v1/auth/users/?search=zhangwei', **self.admin_headers)
        assert resp.status_code == 200
        results = resp.json()['results']
        assert any(u['username'] == 'zhangwei' for u in results)

    @pytest.mark.integration
    def test_list_filter_by_department_and_status(self):
        """?department_id= 与 ?status= 组合筛选"""
        _create_user(username='deptb_user', department=self.dept_b)
        _create_user(username='disabled_user2', status='disabled', department=self.dept_a)
        # 部门 A 且正常
        resp = self._list(f'/api/v1/auth/users/?department_id={self.dept_a.id}&status=active',
                          **self.admin_headers)
        results = resp.json()['results']
        assert all(u['department_id'] == self.dept_a.id for u in results)
        assert all(u['status'] == 'active' for u in results)
        # 部门 B
        resp_b = self._list(f'/api/v1/auth/users/?department_id={self.dept_b.id}', **self.admin_headers)
        assert all(u['department_id'] == self.dept_b.id for u in resp_b.json()['results'])

    @pytest.mark.integration
    def test_list_filter_by_team_and_role(self):
        """?team_id= 与 ?role_id= 筛选（role_id 走 user_role_rels 关联）"""
        _create_user(username='team_b_user', team=self.team_b, department=self.dept_a)
        viewer = _get_or_create_role('viewer')
        _grant_global_role(self.normal_user, 'viewer')
        resp = self._list(f'/api/v1/auth/users/?team_id={self.team_b.id}', **self.admin_headers)
        assert all(u['team']['id'] == self.team_b.id for u in resp.json()['results'])
        resp_role = self._list(f'/api/v1/auth/users/?role_id={viewer.id}', **self.admin_headers)
        assert any(u['username'] == 'normal' for u in resp_role.json()['results'])

    @pytest.mark.integration
    def test_list_invalid_filter_params_ignored(self):
        """非数字的 department_id/team_id/role_id 应被安全忽略（不抛 500）"""
        resp = self._list('/api/v1/auth/users/?department_id=abc&team_id=xyz&role_id=!@#',
                          **self.admin_headers)
        assert resp.status_code == 200

    @pytest.mark.integration
    def test_list_pagination(self):
        """分页参数生效：page_size 控制每页条数，响应含 count"""
        for i in range(5):
            _create_user(username=f'page_user{i}', email=f'page{i}@test.com')
        resp = self._list('/api/v1/auth/users/?page=1&page_size=2', **self.admin_headers)
        data = resp.json()
        assert 'count' in data and 'results' in data
        assert len(data['results']) <= 2

    @pytest.mark.integration
    def test_dept_manager_only_sees_own_dept(self):
        """部门经理列表只能看到本部门（含属地授权部门）用户 —— 数据范围 DEPT 分支"""
        _create_user(username='deptb_only', department=self.dept_b)
        resp = self._list('/api/v1/auth/users/', **self.dept_mgr_headers)
        assert resp.status_code == 200
        results = resp.json()['results']
        # 至少看到本部门用户，且看不到部门 B 用户
        assert any(u['username'] == 'deptmgr' for u in results)
        assert all(u['department_id'] == self.dept_a.id for u in results)

    @pytest.mark.integration
    def test_team_leader_only_sees_own_team(self):
        """团队组长列表只能看到本团队（含属地授权团队）用户 —— 数据范围 TEAM 分支"""
        _create_user(username='team_b_only', team=self.team_b, department=self.dept_a)
        _create_user(username='no_team_user')
        resp = self._list('/api/v1/auth/users/', **self.leader_headers)
        assert resp.status_code == 200
        results = resp.json()['results']
        assert any(u['username'] == 'leader' for u in results)
        assert all(u.get('team') is None or u['team']['id'] == self.team_a.id for u in results)


# ============================================================================
# UserViewSet 创建 / 编辑 —— 角色、团队、唯一性、越权
# ============================================================================

class TestUserViewSetCreateUpdate(UsersAPIExtraBase):
    """UserViewSet create/update 的角色处理与权限边界"""

    @pytest.mark.integration
    def test_create_with_roles_and_team(self):
        """超管创建用户并分配角色 + 团队 → 201，角色关联与 team_id 落库"""
        viewer = _get_or_create_role('viewer')
        resp = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({
                'username': 'newuser1', 'email': 'newuser1@test.com',
                'real_name': '新用户', 'department_id': self.dept_a.id,
                'role_ids': [viewer.id], 'team_ids': [self.team_a.id],
            }),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 201
        user = User.objects.get(username='newuser1')
        assert user.team_id == self.team_a.id
        assert UserRoleRel.objects.filter(user=user, role=viewer, status='ACTIVE').exists()

    @pytest.mark.integration
    def test_create_without_password_uses_default(self):
        """未传密码时使用加密安全随机默认密码（12 位，含大小写+数字+特殊字符）"""
        resp = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({'username': 'DefaultPwdUser', 'email': 'dp@test.com'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 201
        user = User.objects.get(username='DefaultPwdUser')
        # 默认密码为随机生成，验证密码已设置（非空且可用）
        assert user.password  # 密码哈希不为空

    @pytest.mark.integration
    def test_create_duplicate_active_email_400(self):
        """邮箱命中活跃用户 → 400（仅命中已删除用户才走 409 恢复）"""
        resp = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({'username': 'dup_email', 'email': self.normal_user.email}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_duplicate_username_400(self):
        """username 冲突不提供恢复 → 400"""
        resp = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({'username': 'normal', 'email': 'another@test.com'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_missing_email_400(self):
        """缺必填 email → 400"""
        resp = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({'username': 'no_email_user'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_dept_manager_duplicate_role_400(self):
        """同一部门已存在部门经理时，再创建该部门经理 → 400（_validate_role_uniqueness）

        基类中的 dept_mgr 已占用部门 A，此处用部门 B 验证唯一性校验。
        """
        dm_role = _get_or_create_role('dept_manager')
        # 第一个部门经理（部门 B）创建成功
        resp1 = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({
                'username': 'dm1', 'email': 'dm1@test.com',
                'department_id': self.dept_b.id, 'role_ids': [dm_role.id],
            }),
            content_type='application/json', **self.admin_headers,
        )
        assert resp1.status_code == 201
        # 第二个同部门部门经理 → 400
        resp2 = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({
                'username': 'dm2', 'email': 'dm2@test.com',
                'department_id': self.dept_b.id, 'role_ids': [dm_role.id],
            }),
            content_type='application/json', **self.admin_headers,
        )
        assert resp2.status_code == 400
        assert '已有部门经理' in resp2.json().get('detail', '')

    @pytest.mark.integration
    def test_create_team_leader_duplicate_400(self):
        """同一团队已存在 team_leader 时再分配 → 400

        基类中的 leader 已占用团队 A，此处用团队 B 验证唯一性校验。
        """
        tl_role = _get_or_create_role('team_leader')
        resp1 = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({
                'username': 'tl1', 'email': 'tl1@test.com',
                'department_id': self.dept_a.id,
                'role_ids': [tl_role.id], 'team_ids': [self.team_b.id],
            }),
            content_type='application/json', **self.admin_headers,
        )
        assert resp1.status_code == 201
        resp2 = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({
                'username': 'tl2', 'email': 'tl2@test.com',
                'department_id': self.dept_a.id,
                'role_ids': [tl_role.id], 'team_ids': [self.team_b.id],
            }),
            content_type='application/json', **self.admin_headers,
        )
        assert resp2.status_code == 400
        assert '已有团队组长' in resp2.json().get('detail', '')

    @pytest.mark.integration
    def test_team_leader_create_locks_own_team(self):
        """团队组长创建用户：department 被锁定为本部门，指定其他团队 → 403"""
        resp = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({
                'username': 'byleader', 'email': 'bl@test.com',
                'team_ids': [self.team_b.id],
            }),
            content_type='application/json', **self.leader_headers,
        )
        assert resp.status_code == 403
        assert '只能分配到自己的团队' in resp.json().get('detail', '')

    @pytest.mark.integration
    def test_team_leader_create_without_team_uses_own(self):
        """团队组长创建用户未传 team_ids → 默认归入自己的团队"""
        resp = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({'username': 'byleader2', 'email': 'bl2@test.com'}),
            content_type='application/json', **self.leader_headers,
        )
        assert resp.status_code == 201
        user = User.objects.get(username='byleader2')
        assert user.team_id == self.team_a.id
        assert user.department_id == self.dept_a.id

    @pytest.mark.integration
    def test_dept_manager_create_drops_super_admin_role(self):
        """部门经理创建用户时，高级角色被 _filter_downward_roles 过滤掉（不会越权提权）"""
        sa_role = _get_or_create_role('super_admin')
        resp = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({
                'username': 'bydeptmgr', 'email': 'bdm@test.com',
                'department_id': self.dept_a.id, 'role_ids': [sa_role.id],
            }),
            content_type='application/json', **self.dept_mgr_headers,
        )
        assert resp.status_code == 201
        user = User.objects.get(username='bydeptmgr')
        # super_admin 角色被过滤，用户不是超管
        assert user.is_super_admin is False
        assert not UserRoleRel.objects.filter(user=user, role=sa_role).exists()

    @pytest.mark.integration
    def test_dept_manager_create_other_dept_403(self):
        """部门经理创建用户指定其他部门 → 403（越权拦截，只能在本部门/授权部门内建人）"""
        resp = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({
                'username': 'bdm_deptb', 'email': 'bdm_deptb@test.com',
                'department_id': self.dept_b.id,
            }),
            content_type='application/json', **self.dept_mgr_headers,
        )
        assert resp.status_code == 403
        assert '无权在该部门创建用户' in resp.json().get('detail', '')

    @pytest.mark.integration
    def test_dept_manager_create_other_dept_team_403(self):
        """部门经理创建用户指定其他部门团队 → 403（越权拦截）"""
        other_team = Team.objects.create(name='市场一组', code='mkt_t1', department=self.dept_b)
        resp = self.client.post(
            '/api/v1/auth/users/',
            data=json.dumps({
                'username': 'bdm_team', 'email': 'bdm_team@test.com',
                'department_id': self.dept_a.id, 'team_ids': [other_team.id],
            }),
            content_type='application/json', **self.dept_mgr_headers,
        )
        assert resp.status_code == 403
        assert '只能分配到本部门下的团队' in resp.json().get('detail', '')

    @pytest.mark.integration
    def test_dept_manager_update_other_dept_team_403(self):
        """部门经理编辑用户把团队改成其他部门团队 → 403（越权拦截）"""
        other_team = Team.objects.create(name='市场一组', code='mkt_t2', department=self.dept_b)
        target = _create_user(username='bdm_update', team=self.team_a, department=self.dept_a)
        resp = self.client.patch(
            f'/api/v1/auth/users/{target.id}/',
            data=json.dumps({'team_ids': [other_team.id]}),
            content_type='application/json', **self.dept_mgr_headers,
        )
        assert resp.status_code == 403
        assert '只能分配到本部门下的团队' in resp.json().get('detail', '')
        # 未被部分写入：团队保持原样
        target.refresh_from_db()
        assert target.team_id == self.team_a.id

    @pytest.mark.integration
    def test_update_with_roles_revokes_old_ones(self):
        """超管更新用户角色 → 旧角色置 REVOKED，新角色置 ACTIVE"""
        viewer = _get_or_create_role('viewer')
        contributor = _get_or_create_role('contributor')
        _grant_global_role(self.normal_user, 'contributor')
        resp = self.client.patch(
            f'/api/v1/auth/users/{self.normal_user.id}/',
            data=json.dumps({'role_ids': [viewer.id]}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        assert UserRoleRel.objects.filter(
            user=self.normal_user, role=contributor, status='REVOKED').exists()
        assert UserRoleRel.objects.filter(
            user=self.normal_user, role=viewer, status='ACTIVE').exists()

    @pytest.mark.integration
    def test_update_status_change_by_admin(self):
        """超管直接通过 update 改状态（status 字段）→ 200"""
        target = _create_user(username='status_change', email='sc@test.com')
        resp = self.client.patch(
            f'/api/v1/auth/users/{target.id}/',
            data=json.dumps({'status': 'disabled'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        target.refresh_from_db()
        assert target.status == 'disabled'

    @pytest.mark.integration
    def test_update_invalid_department_400(self):
        """更新到不存在的部门 → 400（UserUpdateSerializer.validate_department_id）"""
        resp = self.client.patch(
            f'/api/v1/auth/users/{self.normal_user.id}/',
            data=json.dumps({'department_id': 999999}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_dept_manager_edit_own_dept_user_200(self):
        """部门经理可编辑本部门用户（_check_user_manage DEPT 分支放行）"""
        target = _create_user(username='in_dept_a', email='ida@test.com', department=self.dept_a)
        resp = self.client.patch(
            f'/api/v1/auth/users/{target.id}/',
            data=json.dumps({'real_name': '改名字'}),
            content_type='application/json', **self.dept_mgr_headers,
        )
        assert resp.status_code == 200
        target.refresh_from_db()
        assert target.real_name == '改名字'

    @pytest.mark.integration
    def test_dept_manager_edit_other_dept_user_404(self):
        """部门经理编辑他部门用户 → 404（数据范围过滤后 get_object 找不到资源）

        部门经理 queryset 已按属地过滤为本部门，他部门用户不可见，
        DRF 对不可见资源返回 404 而非 403（对外不暴露资源存在性）。
        """
        target = _create_user(username='in_dept_b', email='idb@test.com', department=self.dept_b)
        resp = self.client.patch(
            f'/api/v1/auth/users/{target.id}/',
            data=json.dumps({'real_name': '越权'}),
            content_type='application/json', **self.dept_mgr_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_team_leader_edit_other_team_404(self):
        """团队组长编辑其他团队用户 → 404（数据范围过滤后资源不可见）"""
        target = _create_user(username='in_team_b', email='itb@test.com', team=self.team_b,
                              department=self.dept_a)
        resp = self.client.patch(
            f'/api/v1/auth/users/{target.id}/',
            data=json.dumps({'real_name': '越权'}),
            content_type='application/json', **self.leader_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_team_leader_cannot_change_department(self):
        """团队组长编辑用户时改部门 → 403（无权修改部门）"""
        target = _create_user(username='in_team_a2', email='ita2@test.com', team=self.team_a,
                              department=self.dept_a)
        resp = self.client.patch(
            f'/api/v1/auth/users/{target.id}/',
            data=json.dumps({'department_id': self.dept_b.id}),
            content_type='application/json', **self.leader_headers,
        )
        assert resp.status_code == 403
        assert '无权修改部门' in resp.json().get('detail', '')

    @pytest.mark.integration
    def test_team_leader_cannot_assign_other_team(self):
        """团队组长把用户分配到其他团队 → 403（只能分配到自己的团队）"""
        target = _create_user(username='in_team_a3', email='ita3@test.com', team=self.team_a,
                              department=self.dept_a)
        resp = self.client.patch(
            f'/api/v1/auth/users/{target.id}/',
            data=json.dumps({'team_ids': [self.team_b.id]}),
            content_type='application/json', **self.leader_headers,
        )
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_update_nonexistent_user_404(self):
        """更新不存在的用户 → 404"""
        resp = self.client.patch(
            '/api/v1/auth/users/999999/',
            data=json.dumps({'real_name': 'x'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 404


# ============================================================================
# UserViewSet 自定义 action —— assign_roles / revive / permission_detail
# ============================================================================

class TestUserViewSetActionsExtra(UsersAPIExtraBase):
    """UserViewSet 补充 action：assign_roles / revive / permission_detail / toggle 边界"""

    @pytest.mark.integration
    def test_assign_roles_super_admin_200(self):
        """超管通过 assign_roles 分配角色 → 200"""
        viewer = _get_or_create_role('viewer')
        resp = self.client.post(
            f'/api/v1/auth/users/{self.normal_user.id}/assign_roles/',
            data=json.dumps({'role_ids': [viewer.id]}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['ok'] is True
        assert UserRoleRel.objects.filter(user=self.normal_user, role=viewer, status='ACTIVE').exists()

    @pytest.mark.integration
    def test_assign_roles_normal_user_403(self):
        """非超管调用 assign_roles → 403（仅超级管理员可分配角色）"""
        viewer = _get_or_create_role('viewer')
        resp = self.client.post(
            f'/api/v1/auth/users/{self.normal_user.id}/assign_roles/',
            data=json.dumps({'role_ids': [viewer.id]}),
            content_type='application/json', **self.normal_headers,
        )
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_assign_roles_nonexistent_user_404(self):
        """assign_roles 目标用户不存在 → 404"""
        resp = self.client.post(
            '/api/v1/auth/users/999999/assign_roles/',
            data=json.dumps({'role_ids': []}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_revive_deleted_user_200(self):
        """软删用户可通过 revive 恢复：清除删除标记，重置 viewer 兜底角色"""
        target = _create_user(username='revive_me', email='rm@test.com')
        target.is_deleted = True
        target.deleted_at = target.created_at
        target.save()
        resp = self.client.post(
            f'/api/v1/auth/users/{target.id}/revive/',
            data=json.dumps({'real_name': '复活后姓名'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        target.refresh_from_db()
        assert target.is_deleted is False
        assert target.deleted_at is None
        assert target.real_name == '复活后姓名'
        viewer = _get_or_create_role('viewer')
        assert UserRoleRel.objects.filter(user=target, role=viewer, status='ACTIVE').exists()

    @pytest.mark.integration
    def test_revive_active_user_404(self):
        """revive 未删除的用户 → 404（统一返回 404 防止用户枚举）"""
        resp = self.client.post(
            f'/api/v1/auth/users/{self.normal_user.id}/revive/',
            data=json.dumps({}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_revive_nonexistent_404(self):
        """revive 不存在的用户 → 404"""
        resp = self.client.post(
            '/api/v1/auth/users/999999/revive/',
            data=json.dumps({}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_permission_detail_rows(self):
        """permission_detail 返回扁平行列表：团队兜底 viewer + 部门授权 + 全局角色"""
        contributor = _get_or_create_role('contributor')
        # 团队属地授权：dept_a 下 team_b 的 contributor
        UserTeamScopeRel.objects.create(
            user=self.normal_user, role=contributor, team=self.team_b,
            status=GrantStatus.ACTIVE,
        )
        # 全局角色：contributor
        _grant_global_role(self.normal_user, 'contributor')
        resp = self.client.get(
            f'/api/v1/auth/users/{self.normal_user.id}/permission-detail/', **self.admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['user']['id'] == self.normal_user.id
        rows = data['rows']
        # 人事归属团队（team_a 无显式授权）→ 补 viewer 兜底行
        assert any(r['role_code'] == 'viewer' and r['team_name'] == '后端组' for r in rows)
        # 显式团队授权行（team_b contributor）
        assert any(r['role_code'] == 'contributor' and r['team_name'] == '前端组' for r in rows)
        # 全局角色行（全部/全部）
        assert any(r['dept_name'] == '全部' and r['role_code'] == 'contributor' for r in rows)

    @pytest.mark.integration
    def test_toggle_status_super_admin_forbidden(self):
        """toggle_status 作用于超管 → 403（_check_can_manage_user 规则3）"""
        resp = self.client.post(
            f'/api/v1/auth/users/{self.super_admin.id}/toggle_status/', **self.admin_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_toggle_status_self_forbidden(self):
        """toggle_status 作用于自己 → 403（_check_can_manage_user 规则2）"""
        resp = self.client.post(
            f'/api/v1/auth/users/{self.dept_mgr.id}/toggle_status/', **self.dept_mgr_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_batch_delete_skips_super_admin(self):
        """批量删除混合超管：超管跳过，仅删除普通用户（返回实际删除数）"""
        target = _create_user(username='batch_target', email='bt@test.com')
        resp = self.client.post(
            '/api/v1/auth/users/batch_delete/',
            data=json.dumps({'ids': [self.super_admin.id, target.id]}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['deleted'] == 1
        target.refresh_from_db()
        assert target.is_deleted is True
        self.super_admin.refresh_from_db()
        assert self.super_admin.is_deleted is False

    @pytest.mark.integration
    def test_batch_delete_no_valid_ids_403(self):
        """批量删除全是超管/无可用目标 → 403"""
        resp = self.client.post(
            '/api/v1/auth/users/batch_delete/',
            data=json.dumps({'ids': [self.super_admin.id]}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 403


# ============================================================================
# UserViewSet 导出 / 导入 / 表单选项
# ============================================================================

class TestUserViewSetExportImport(UsersAPIExtraBase):
    """export / batch_export / batch_import / import_template / form_options"""

    @pytest.mark.integration
    def test_export_single_user_csv(self):
        """导出单个用户 CSV

        修复前 export 把 [u] 列表传给 _export_users_csv，内部对入参调用
        .select_related()（仅 QuerySet 支持）导致 500；修复后传 QuerySet，正常返回 200。
        """
        client = Client(raise_request_exception=False)
        resp = client.get(
            f'/api/v1/auth/users/{self.normal_user.id}/export/', **self.admin_headers)
        assert resp.status_code == 200
        assert 'normal' in resp.content.decode('utf-8-sig')

    @pytest.mark.integration
    def test_batch_export_selected_ids(self):
        """按 ids 批量导出 CSV"""
        u2 = _create_user(username='exp2', email='exp2@test.com')
        resp = self.client.post(
            '/api/v1/auth/users/batch_export/',
            data=json.dumps({'ids': [self.normal_user.id, u2.id]}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        content = resp.content.decode('utf-8-sig')
        assert 'exp2' in content and 'normal' in content

    @pytest.mark.integration
    def test_batch_export_without_ids_exports_all(self):
        """不传 ids 时导出当前可见用户全量（filter_queryset 后）"""
        resp = self.client.post(
            '/api/v1/auth/users/batch_export/',
            data=json.dumps({}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        content = resp.content.decode('utf-8-sig')
        assert 'normal' in content

    @pytest.mark.integration
    def test_import_template_download(self):
        """下载导入模板 CSV（含表头与示例行）"""
        resp = self.client.get('/api/v1/auth/users/import_template/', **self.admin_headers)
        assert resp.status_code == 200
        assert 'text/csv' in resp['Content-Type']
        content = resp.content.decode('utf-8-sig')
        assert '用户名,姓名,邮箱' in content

    def _csv_upload(self, csv_text, filename='users.csv', headers=None):
        """构造 CSV 文件并 POST batch_import（headers 为 None 时表示匿名请求）"""
        file_bytes = csv_text.encode('utf-8-sig')
        f = SimpleUploadedFile(filename, file_bytes, content_type='text/csv')
        return self.client.post(
            '/api/v1/auth/users/batch_import/',
            data={'file': f},
            **(headers or {}),
        )

    @pytest.mark.integration
    def test_batch_import_success(self):
        """合法 CSV 批量导入 → 返回结果 CSV，成功计数在响应头中"""
        resp = self._csv_upload(
            '用户名,姓名,邮箱,部门,团队,状态\n'
            'zhangsan,张三,zhangsan@test.com,研发部,后端组,启用\n',
            headers=self.admin_headers,
        )
        assert resp.status_code == 200
        assert resp['X-Import-Success'] == '1'
        assert resp['X-Import-Fail'] == '0'
        user = User.objects.get(username='zhangsan')
        assert user.department_id == self.dept_a.id
        assert user.team_id == self.team_a.id
        assert user.status == 'active'
        # 导入用户默认 viewer 角色
        viewer = _get_or_create_role('viewer')
        assert UserRoleRel.objects.filter(user=user, role=viewer, status='ACTIVE').exists()
        # 响应体为结果 CSV（含 结果/原因 列）
        result = resp.content.decode('utf-8-sig')
        assert '成功' in result

    @pytest.mark.integration
    def test_batch_import_duplicate_username_fail_row(self):
        """CSV 中存在已占用用户名 → 该行失败并写明原因，不影响整体响应"""
        resp = self._csv_upload(
            '用户名,姓名,邮箱,部门,团队,状态\n'
            'normal,重复用户,dup@test.com,研发部,后端组,启用\n',
            headers=self.admin_headers,
        )
        assert resp.status_code == 200
        assert resp['X-Import-Success'] == '0'
        assert resp['X-Import-Fail'] == '1'
        result = resp.content.decode('utf-8-sig')
        assert '已存在' in result

    @pytest.mark.integration
    def test_batch_import_missing_file_400(self):
        """未上传文件 → 400"""
        resp = self.client.post('/api/v1/auth/users/batch_import/', data={}, **self.admin_headers)
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_batch_import_wrong_extension_400(self):
        """非 .csv 扩展名 → 400"""
        resp = self._csv_upload('用户名,姓名\nx,y\n', filename='users.txt', headers=self.admin_headers)
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_batch_import_missing_required_column_400(self):
        """CSV 缺少必填列（姓名）→ 400"""
        resp = self._csv_upload('用户名,邮箱\nx,x@test.com\n', headers=self.admin_headers)
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_batch_import_normal_user_403(self):
        """普通用户无用户管理权限批量导入 → 403"""
        resp = self._csv_upload(
            '用户名,姓名,邮箱\nx,某人,x@test.com\n', headers=self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_batch_import_anonymous_401(self):
        """匿名批量导入 → 401"""
        resp = self._csv_upload('用户名,姓名,邮箱\nx,某人,x@test.com\n')
        assert resp.status_code in (401, 403)

    @pytest.mark.integration
    def test_form_options_super_admin(self):
        """超管 form_options 返回全部角色与可分配角色"""
        resp = self.client.get('/api/v1/auth/users/form_options/', **self.admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert any(d['name'] == '研发部' for d in data['departments'])
        assert any(d['name'] == '后端组' for d in data['teams'])
        assert len(data['roles']) >= 1 and len(data['assignable_roles']) >= 1

    @pytest.mark.integration
    def test_form_options_normal_user_403(self):
        """普通用户访问 form_options → 403（UserViewSet 类级权限 CanManageUsers 拦截）"""
        resp = self.client.get('/api/v1/auth/users/form_options/', **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_search_with_dept_filter(self):
        """search action 支持 department_id 过滤"""
        resp = self.client.get(
            f'/api/v1/auth/users/search/?q=&department_id={self.dept_a.id}', **self.admin_headers)
        assert resp.status_code == 200
        users = resp.json()['users']
        assert all(u['department_id'] == self.dept_a.id for u in users)


# ============================================================================
# DepartmentViewSet —— 部门 CRUD
# ============================================================================

