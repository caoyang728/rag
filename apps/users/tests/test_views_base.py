"""
apps.users.views 补充测试公共基座 —— 共享辅助函数 / FakeRedis / UsersAPIExtraBase

供 test_views_auth / test_views_user / test_views_org / test_views_rbac /
test_views_ticket 复用，避免各文件重复造基类。

测试约定：
- pytest-django（django_db）+ 真实 DB：权限链路（CanManageUsers / 数据范围）必须端到端验证
- JWT：RefreshToken.for_user(user).access_token
- 外部依赖 mock：Redis（密码重置验证码）、邮件发送、图形验证码
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
from apps.users.ticket_service import create_ticket, approve_ticket
from apps.users.tests.test_views import UsersAPITestBase


def _get_or_create_role(role_key, **defaults):
    """获取或创建内置角色，补齐默认字段（角色类型/数据范围与业务定义对齐）"""
    default_map = {
        'super_admin': dict(name='超级管理员', is_builtin=True,
                            role_type=RoleType.GLOBAL, data_scope=DataScope.GLOBAL),
        'viewer': dict(name='查看者', is_builtin=True,
                       role_type=RoleType.NORMAL_USER, data_scope=DataScope.TEAM),
        'contributor': dict(name='贡献者', is_builtin=True,
                            role_type=RoleType.NORMAL_USER, data_scope=DataScope.TEAM),
        'dept_manager': dict(name='部门经理', is_builtin=True,
                             role_type=RoleType.DEPT_SCOPE, data_scope=DataScope.DEPT),
        'team_leader': dict(name='团队组长', is_builtin=True,
                            role_type=RoleType.TEAM_SCOPE, data_scope=DataScope.TEAM),
    }
    defaults = {**default_map.get(role_key, {}), **defaults}
    role, _ = Role.objects.get_or_create(role_key=role_key, defaults=defaults)
    return role


def _create_user(username, password='pass12345', **extra):
    """创建测试用户（email 默认 {username}@test.com）"""
    extra.setdefault('email', f'{username}@test.com')
    return User.objects.create_user(username=username, password=password, **extra)


def _grant_permission(role, perm_key):
    """给角色绑定权限点（RolePermissionRel is_active=True），构建 RBAC 链路"""
    perm, _ = Permission.objects.get_or_create(
        permission_key=perm_key,
        defaults={'permission_name': perm_key, 'module': perm_key.split('.')[0]},
    )
    RolePermissionRel.objects.get_or_create(role=role, permission=perm)


def _grant_global_role(user, role_key, status=GrantStatus.ACTIVE):
    """给用户授予全局角色（UserRoleRel）"""
    role = _get_or_create_role(role_key)
    rel, _ = UserRoleRel.objects.update_or_create(
        user=user, role=role, defaults={'status': status})
    return rel


def _auth_headers(user):
    """生成 JWT Bearer header"""
    refresh = RefreshToken.for_user(user)
    return {'HTTP_AUTHORIZATION': f'Bearer {str(refresh.access_token)}'}


class FakeRedis:
    """内存版 Redis 客户端 —— 密码重置/图形验证码等依赖 Redis 的接口测试用

    支持 setex/get/delete 三个实际调用点，.data 暴露底层字典供断言读取。
    初始数据通过构造参数注入（如 {'pwd_reset:normal@test.com': '123456'}）。
    """

    def __init__(self, data=None):
        self.data = dict(data or {})

    def setex(self, key, ttl, value):
        self.data[key] = value

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.data.pop(key, None)

    def exists(self, key):
        return key in self.data

    def ttl(self, key):
        # 密码重置防刷依赖 ttl 判断距上次发送时间(300s 过期,>240s 视为 1 分钟内)
        return 300 if key in self.data else -2


class UsersAPIExtraBase(UsersAPITestBase):
    """用户 API 补充测试基座 —— 在 UsersAPITestBase 基础上扩展组织架构与管理者身份

    扩展内容:
    - 部门:dept_a(研发部)/dept_b(市场部);团队:team_a(后端组)/team_b(前端组,均属 dept_a)
    - normal_user 归属 team_a(跨团队申请的目标团队为 team_b)
    - team_leader(leader):team_a 组长,Team.leader_id + UserTeamScopeRel(team_leader) 双重身份
    - dept_mgr(deptmgr):dept_manager 授权挂 UserTeamScopeRel(team_a),自身归属 dept_a
      (部门经理身份以"范围授权"为准,_get_dept_leader_id 仅识别 dept 属地/全局匹配,
       故 dept_a 视为无部门经理,跨团队 viewer 申请链可降级到超管单审)
    - dept_mgr_headers / leader_headers
    """

    @pytest.fixture(autouse=True)
    def _env(self):
        """先执行父级公共环境(超管/普通用户/基础部门/JWT header),再补组织与管理者"""
        self._setup_env()

        # 管理角色预建 + 授予 user.manage(UserViewSet 类级权限 CanManageUsers 依赖)
        dept_manager_role = _get_or_create_role('dept_manager')
        team_leader_role = _get_or_create_role('team_leader')
        _grant_permission(dept_manager_role, 'user.manage')
        _grant_permission(team_leader_role, 'user.manage')

        # 组织架构:研发部下挂后端/前端两组,另设市场部
        self.dept_a = Department.objects.create(name='研发部', code='rd')
        self.dept_b = Department.objects.create(name='市场部', code='mk')
        self.team_a = Team.objects.create(name='后端组', code='rd_backend', department=self.dept_a)
        self.team_b = Team.objects.create(name='前端组', code='rd_frontend', department=self.dept_a)

        # 普通用户归属 team_a(跨团队申请:本团队 team_a → 目标团队 team_b)
        self.normal_user.team = self.team_a
        self.normal_user.department = self.dept_a
        self.normal_user.save(update_fields=['team', 'department'])

        # 团队组长:leader_id 字段 + 团队属地授权(待审批列表按 leader_id 匹配)
        self.team_leader = _create_user('leader', team=self.team_a, department=self.dept_a)
        UserTeamScopeRel.objects.create(
            user=self.team_leader, role=team_leader_role, team=self.team_a,
            status=GrantStatus.ACTIVE)
        self.team_a.leader = self.team_leader
        self.team_a.save(update_fields=['leader'])

        # 部门经理:dept_manager 授权挂到 team_a(范围授权表),自身归属 dept_a
        self.dept_mgr = _create_user('deptmgr', department=self.dept_a)
        UserTeamScopeRel.objects.create(
            user=self.dept_mgr, role=dept_manager_role, team=self.team_a,
            status=GrantStatus.ACTIVE)

        self.dept_mgr_headers = _auth_headers(self.dept_mgr)
        self.leader_headers = _auth_headers(self.team_leader)

    def _team_c(self):
        """获取/创建市场部下团队(跨部门申请场景)—— 惰性创建,避免污染其他测试的组织架构

        新矩阵(资源所有者审批)下,同部门跨团队 viewer/contributor 申请自动生效(空链),
        跨部门申请才产生审批链,故待审批工单统一以市场一组(属 dept_b)为目标组织。
        """
        team, _ = Team.objects.get_or_create(
            code='mkt_1', defaults={'name': '市场一组', 'department': self.dept_b})
        return team

    def _create_pending_ticket(self, applicant=None):
        """创建跨部门 viewer 待审批工单 —— 供工单视图测试复用

        场景:applicant(归属 dept_a) 申请 dept_b 下 team_c(市场一组) 查看者。
        新矩阵(资源所有者审批)下,同部门跨团队自动生效,跨部门才产生审批链;
        team_c 所属部门(dept_b)无部门经理且无用户管理员,审批链降级为超管单审,
        便于测试用超管直接审批/驳回,并验证 EXECUTED 后授权写入。
        """
        applicant = applicant or self.normal_user
        return create_ticket(
            applicant=applicant,
            target_user=applicant,
            change_type=TicketChangeType.GRANT,
            role=_get_or_create_role('viewer'),
            scope_type=ScopeType.TEAM,
            scope_id=self._team_c().id,
            reason='跨部门查看市场一组资料',
        )
