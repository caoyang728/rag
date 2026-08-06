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
