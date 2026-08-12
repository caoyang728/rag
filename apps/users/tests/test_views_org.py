"""
apps.users.views 组织架构测试 —— 部门 / 团队增删改走组织变更工单

与 test_views.py 互补：DepartmentViewSet / TeamViewSet 的写接口（增/删/改）
不再直接 CRUD，而是创建 org 审批工单（TicketOrgDetail），审批通过后由
_execute_org_change 落库生效。因此写接口用例采用"提交 → 审批 → 执行生效"
三段式断言；删除预检（部门下有用户/团队、团队下有成员）仍在创建工单前返回 400。

审批链约定：
- 新增/编辑：normal 单审（USER_ADMIN，持有 user_admin 角色）
- 删除：high 双审（USER_ADMIN 审核 + SUPER_ADMIN 复核，双人独立性）
"""
import json

import pytest
from django.test import Client

from apps.users.models import (
    User, Department, Team,
    TicketList, TicketStatus, TicketBizType,
)
from apps.users.tests.test_views_base import (
    _create_user, _grant_global_role, UsersAPIExtraBase,
)

# 统一审批入口（users urls 挂 /api/v1/auth/ 前缀下）
TICKET_API = '/api/v1/auth/tickets/'
DEPT_API = '/api/v1/auth/departments/'
TEAM_API = '/api/v1/auth/teams/'


class OrgTicketTestBase(UsersAPIExtraBase):
    """组织工单测试基座 —— 提供 user_admin 审批人身份与审批/复核辅助"""

    def _make_user_admin(self, username='org_ua'):
        """创建持有 user_admin 角色的审批人（单审节点审批者）"""
        ua = _create_user(username)
        _grant_global_role(ua, 'user_admin')
        return ua

    def _approve(self, ticket, user_headers, comment='同意'):
        """审批工单当前节点（HTTP 统一审批入口）"""
        return self.client.post(
            TICKET_API + f'{ticket.id}/approve/',
            data=json.dumps({'comment': comment}),
            content_type='application/json', **user_headers)


class TestDepartmentViewSet(OrgTicketTestBase):
    """部门写接口：创建工单 + 审批后落库 + 删除预检"""

    def _new_ticket_asserts(self, resp, risk_level='normal'):
        """通用断言：写接口返回工单而非直接落库（201，PENDING）"""
        assert resp.status_code == 201
        data = resp.json()
        assert data['ticket_no'].startswith('ZZ')
        assert data['status'] == TicketStatus.PENDING
        assert data['risk_level'] == risk_level
        return data

    @pytest.mark.integration
    def test_list_authenticated_200(self):
        """任何登录用户可查看部门列表（IsAuthenticated，含 user_count/teams 聚合）"""
        resp = self.client.get(DEPT_API, **self.normal_headers)
        assert resp.status_code == 200
        results = resp.json()['results']
        assert any(d['name'] == '研发部' for d in results)
        rd = next(d for d in results if d['name'] == '研发部')
        assert rd['user_count'] >= 1
        assert any(t['name'] == '后端组' for t in rd['teams'])
        # 嵌套团队携带成员数量（用户需求：团队名后显示成员数）
        ba = next(t for t in rd['teams'] if t['name'] == '后端组')
        assert 'user_count' in ba

    @pytest.mark.integration
    def test_list_anonymous_401(self):
        """匿名查看部门列表 → 401"""
        resp = self.client.get(DEPT_API, **self.anon_headers)
        assert resp.status_code in (401, 403)

    @pytest.mark.integration
    def test_create_by_admin_returns_ticket(self):
        """超管创建部门 → 201 返回工单（PENDING 单审），不直接落库

        组织变更一律走工单：创建工单时预检（名称唯一、编码自动生成），
        审批通过后由 _execute_org_change 创建部门。
        """
        resp = self.client.post(
            DEPT_API,
            data=json.dumps({'name': '测试部X'}),
            content_type='application/json', **self.admin_headers,
        )
        data = self._new_ticket_asserts(resp)
        # 工单详情已记录目标数据（名称/自动编码），部门本身尚未创建
        assert not Department.objects.filter(name='测试部X', is_deleted=False).exists()
        ticket = TicketList.objects.get(ticket_no=data['ticket_no'])
        od = ticket.org_detail
        assert od.org_type == 'dept'
        assert od.operation == 'add'
        assert od.target_data['name'] == '测试部X'
        assert od.target_data['code']  # 创建时已自动生成编码，审批执行后使用

    @pytest.mark.integration
    def test_create_normal_user_403(self):
        """普通用户创建部门 → 403（_check_can_manage_dept 基于 user.manage_all）"""
        resp = self.client.post(
            DEPT_API,
            data=json.dumps({'name': '新部门'}),
            content_type='application/json', **self.normal_headers,
        )
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_create_duplicate_name_400(self):
        """同名部门重复创建 → 400（创建工单前预检）"""
        resp = self.client.post(
            DEPT_API,
            data=json.dumps({'name': '研发部'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400
        assert '已存在' in resp.json().get('detail', '')

    @pytest.mark.integration
    def test_create_with_leader_id_ignored(self):
        """create 传 leader_id → 201 工单（leader 不落库）

        部门经理改由"任命工单"(GRANT dept_manager)设置,组织 CRUD 不再直接写 leader_id。
        """
        resp = self.client.post(
            DEPT_API,
            data=json.dumps({'name': '新部门X', 'leader_id': 999999}),
            content_type='application/json', **self.admin_headers,
        )
        self._new_ticket_asserts(resp)

    @pytest.mark.integration
    def test_create_approve_executes_dept(self):
        """新增部门：提交 → user_admin 审批 → 部门落库生效"""
        resp = self.client.post(
            DEPT_API,
            data=json.dumps({'name': '新部门A'}),
            content_type='application/json', **self.admin_headers,
        )
        ticket = TicketList.objects.get(ticket_no=resp.json()['ticket_no'])
        assert ticket.status == TicketStatus.PENDING
        assert len(ticket.approval_chain) == 1  # 新增=normal 单审

        ua = self._make_user_admin()
        ua_headers = {'HTTP_AUTHORIZATION': 'Bearer ' + _auth_token(ua)}
        aresp = self._approve(ticket, ua_headers)
        assert aresp.status_code == 200, aresp.content

        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.EXECUTED
        dept = Department.objects.get(name='新部门A', is_deleted=False)
        assert dept.code  # 执行时使用创建时自动生成的编码

    @pytest.mark.integration
    def test_create_restores_deleted_dept_after_approve(self):
        """同名部门曾软删 → 提交工单审批后恢复（201 → 审批 → 恢复，不重复创建）"""
        dept = Department.objects.create(name='旧部门', code='old')
        dept.is_deleted = True
        dept.save()
        resp = self.client.post(
            DEPT_API,
            data=json.dumps({'name': '旧部门'}),
            content_type='application/json', **self.admin_headers,
        )
        ticket = TicketList.objects.get(ticket_no=resp.json()['ticket_no'])
        ua = self._make_user_admin()
        ua_headers = {'HTTP_AUTHORIZATION': 'Bearer ' + _auth_token(ua)}
        assert self._approve(ticket, ua_headers).status_code == 200
        dept.refresh_from_db()
        assert dept.is_deleted is False

    @pytest.mark.integration
    def test_update_department_returns_ticket(self):
        """超管更新部门 → 200 工单（PENDING 单审），DB 未变；审批后生效"""
        resp = self.client.patch(
            f'{DEPT_API}{self.dept_a.id}/',
            data=json.dumps({'name': '研发一部'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data['ticket_no'].startswith('ZZ')
        assert data['status'] == TicketStatus.PENDING
        assert data['risk_level'] == 'normal'
        self.dept_a.refresh_from_db()
        assert self.dept_a.name == '研发部'  # 审批前不生效

        ticket = TicketList.objects.get(ticket_no=data['ticket_no'])
        od = ticket.org_detail
        assert od.operation == 'edit'
        assert od.old_data['name'] == '研发部'
        assert od.new_data['name'] == '研发一部'

        ua = self._make_user_admin()
        ua_headers = {'HTTP_AUTHORIZATION': 'Bearer ' + _auth_token(ua)}
        assert self._approve(ticket, ua_headers).status_code == 200
        self.dept_a.refresh_from_db()
        assert self.dept_a.name == '研发一部'

    @pytest.mark.integration
    def test_update_leader_id_ignored(self):
        """PATCH leader_id → 200 工单且不落库(任命工单设置,CRUD 不再直接写)"""
        resp = self.client.patch(
            f'{DEPT_API}{self.dept_a.id}/',
            data=json.dumps({'leader_id': self.normal_user.id}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['ticket_no']
        self.dept_a.refresh_from_db()
        assert self.dept_a.leader_id is None

    @pytest.mark.integration
    def test_destroy_with_users_400(self):
        """部门下仍有用户 → 400 无法删除（创建工单前预检）"""
        resp = self.client.delete(f'{DEPT_API}{self.dept_a.id}/', **self.admin_headers)
        assert resp.status_code == 400
        assert '用户' in resp.json().get('detail', '')

    @pytest.mark.integration
    def test_destroy_with_teams_400(self):
        """部门下仍有团队 → 400 无法删除（需先删除/迁移团队）"""
        empty_dept = Department.objects.create(name='空部门', code='empty')
        Team.objects.create(name='遗留团队', code='legacy', department=empty_dept)
        resp = self.client.delete(f'{DEPT_API}{empty_dept.id}/', **self.admin_headers)
        assert resp.status_code == 400
        assert '团队' in resp.json().get('detail', '')

    @pytest.mark.integration
    def test_destroy_empty_dept_double_approve(self):
        """删除空部门：提交工单（high 双审）→ 审核+复核 → 软删生效"""
        empty_dept = Department.objects.create(name='空部门2', code='empty2')
        resp = self.client.delete(f'{DEPT_API}{empty_dept.id}/', **self.admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['risk_level'] == 'high'  # 删除=高风险双审
        ticket = TicketList.objects.get(ticket_no=data['ticket_no'])
        assert len(ticket.approval_chain) == 2
        empty_dept.refresh_from_db()
        assert empty_dept.is_deleted is False  # 双审完成前不生效

        # 第 1 节点：USER_ADMIN 审核
        ua = self._make_user_admin()
        ua_headers = {'HTTP_AUTHORIZATION': 'Bearer ' + _auth_token(ua)}
        assert self._approve(ticket, ua_headers).status_code == 200
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.PENDING
        assert ticket.current_step == 1

        # 第 2 节点：SUPER_ADMIN 复核（另一超管，回避申请人=admin）
        sa2 = _create_user('sa2_org')
        _grant_global_role(sa2, 'super_admin')
        sa2_headers = {'HTTP_AUTHORIZATION': 'Bearer ' + _auth_token(sa2)}
        assert self._approve(ticket, sa2_headers).status_code == 200
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.EXECUTED
        empty_dept.refresh_from_db()
        assert empty_dept.is_deleted is True

    @pytest.mark.integration
    def test_destroy_normal_user_403(self):
        """普通用户删除部门 → 403"""
        resp = self.client.delete(f'{DEPT_API}{self.dept_b.id}/', **self.normal_headers)
        assert resp.status_code == 403


# ============================================================================
# TeamViewSet —— 团队写接口走工单
# ============================================================================

class TestTeamViewSet(OrgTicketTestBase):
    """团队写接口：创建工单 + 审批后落库 + 删除预检"""

    @pytest.mark.integration
    def test_list_with_department_filter(self):
        """?department_id= 过滤团队（权限申请部门→团队级联选择用）"""
        resp = self.client.get(
            f'{TEAM_API}?department_id={self.dept_a.id}', **self.normal_headers)
        assert resp.status_code == 200
        results = resp.json()['results']
        assert all(t['department_id'] == self.dept_a.id for t in results)

    @pytest.mark.integration
    def test_create_team_returns_ticket(self):
        """超管创建团队 → 201 工单（单审），审批后落库且编码含部门前缀"""
        resp = self.client.post(
            TEAM_API,
            data=json.dumps({'name': '测试组', 'department_id': self.dept_a.id}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data['ticket_no'].startswith('ZZ')
        assert data['status'] == TicketStatus.PENDING
        assert data['risk_level'] == 'normal'
        assert not Team.objects.filter(name='测试组', department=self.dept_a).exists()

        ticket = TicketList.objects.get(ticket_no=data['ticket_no'])
        od = ticket.org_detail
        assert od.org_type == 'team'
        assert od.target_data['department_id'] == self.dept_a.id
        assert od.target_data['code'].startswith('rd_')  # 创建时已生成含部门前缀编码

        ua = self._make_user_admin()
        ua_headers = {'HTTP_AUTHORIZATION': 'Bearer ' + _auth_token(ua)}
        assert self._approve(ticket, ua_headers).status_code == 200
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.EXECUTED
        team = Team.objects.get(name='测试组', department=self.dept_a)
        assert team.code.startswith('rd_')

    @pytest.mark.integration
    def test_create_missing_department_400(self):
        """缺 department_id → 400（部门ID不能为空）"""
        resp = self.client.post(
            TEAM_API,
            data=json.dumps({'name': '无部门团队'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_nonexistent_department_400(self):
        """department_id 不存在 → 400（指定的部门不存在）"""
        resp = self.client.post(
            TEAM_API,
            data=json.dumps({'name': '幽灵团队', 'department_id': 999999}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_duplicate_name_in_dept_400(self):
        """同部门下重名团队 → 400（创建工单前预检）"""
        resp = self.client.post(
            TEAM_API,
            data=json.dumps({'name': '后端组', 'department_id': self.dept_a.id}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 400
        assert '已存在团队' in resp.json().get('detail', '')

    @pytest.mark.integration
    def test_create_normal_user_403(self):
        """普通用户创建团队 → 403（_check_can_manage_team）"""
        resp = self.client.post(
            TEAM_API,
            data=json.dumps({'name': '越权团队', 'department_id': self.dept_a.id}),
            content_type='application/json', **self.normal_headers,
        )
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_create_with_leader_id_ignored(self):
        """创建团队并传 leader_id → 201 工单且组长不落库(任命工单设置,CRUD 不再直接写)"""
        resp = self.client.post(
            TEAM_API,
            data=json.dumps({
                'name': '带组长团队', 'department_id': self.dept_a.id,
                'leader_id': self.normal_user.id,
            }),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()['ticket_no']

    @pytest.mark.integration
    def test_create_restores_deleted_team_after_approve(self):
        """同部门同名团队曾软删 → 提交工单审批后恢复（不重复创建）"""
        team = Team.objects.create(name='旧团队', code='old_team', department=self.dept_a)
        team.is_deleted = True
        team.save()
        resp = self.client.post(
            TEAM_API,
            data=json.dumps({'name': '旧团队', 'department_id': self.dept_a.id}),
            content_type='application/json', **self.admin_headers,
        )
        ticket = TicketList.objects.get(ticket_no=resp.json()['ticket_no'])
        ua = self._make_user_admin()
        ua_headers = {'HTTP_AUTHORIZATION': 'Bearer ' + _auth_token(ua)}
        assert self._approve(ticket, ua_headers).status_code == 200
        team.refresh_from_db()
        assert team.is_deleted is False

    @pytest.mark.integration
    def test_update_team_returns_ticket(self):
        """超管更新团队 → 200 工单；审批后生效"""
        resp = self.client.patch(
            f'{TEAM_API}{self.team_a.id}/',
            data=json.dumps({'description': '新描述'}),
            content_type='application/json', **self.admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data['ticket_no'].startswith('ZZ')
        assert data['status'] == TicketStatus.PENDING
        self.team_a.refresh_from_db()
        assert self.team_a.description is None  # 审批前不生效

        ticket = TicketList.objects.get(ticket_no=data['ticket_no'])
        od = ticket.org_detail
        assert od.operation == 'edit'
        assert od.new_data['description'] == '新描述'
        ua = self._make_user_admin()
        ua_headers = {'HTTP_AUTHORIZATION': 'Bearer ' + _auth_token(ua)}
        assert self._approve(ticket, ua_headers).status_code == 200
        self.team_a.refresh_from_db()
        assert self.team_a.description == '新描述'

    @pytest.mark.integration
    def test_destroy_with_members_400(self):
        """团队下仍有成员 → 400 无法删除（创建工单前预检）"""
        resp = self.client.delete(f'{TEAM_API}{self.team_a.id}/', **self.admin_headers)
        assert resp.status_code == 400
        assert '成员' in resp.json().get('detail', '')

    @pytest.mark.integration
    def test_destroy_empty_team_double_approve(self):
        """删除空团队：提交工单（high 双审）→ 审核+复核 → 软删生效"""
        empty_team = Team.objects.create(name='空团队', code='empty_team', department=self.dept_b)
        resp = self.client.delete(f'{TEAM_API}{empty_team.id}/', **self.admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['risk_level'] == 'high'
        ticket = TicketList.objects.get(ticket_no=data['ticket_no'])
        assert len(ticket.approval_chain) == 2
        empty_team.refresh_from_db()
        assert empty_team.is_deleted is False

        ua = self._make_user_admin()
        ua_headers = {'HTTP_AUTHORIZATION': 'Bearer ' + _auth_token(ua)}
        assert self._approve(ticket, ua_headers).status_code == 200
        sa2 = _create_user('sa2_org_team')
        _grant_global_role(sa2, 'super_admin')
        sa2_headers = {'HTTP_AUTHORIZATION': 'Bearer ' + _auth_token(sa2)}
        assert self._approve(ticket, sa2_headers).status_code == 200
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.EXECUTED
        empty_team.refresh_from_db()
        assert empty_team.is_deleted is True

    @pytest.mark.integration
    def test_destroy_normal_user_403(self):
        """普通用户删除团队 → 403"""
        resp = self.client.delete(f'{TEAM_API}{self.team_b.id}/', **self.normal_headers)
        assert resp.status_code == 403


def _auth_token(user):
    """生成 JWT access token（避免依赖 RefreshToken 导入，统一封装）"""
    from rest_framework_simplejwt.tokens import RefreshToken
    return str(RefreshToken.for_user(user).access_token)
