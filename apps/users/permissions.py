"""
DRF 权限类 - 基于 RBAC（has_permission + scope 检查）

权限判定统一走 RBAC 链路：
  User → UserRole → RolePermission → Permission(code={module}:{action}:{scope})
super_admin 作为设计规定的快路径，在所有权限类中直接放行。

权限范围 (3 级): all / department / team
权限动作: read / upload / manage / download / manage_users / config
"""
from rest_framework.permissions import BasePermission
from apps.users.models import has_permission, has_perm_for_scope


class RequirePerm(BasePermission):
    """基类：子类设置 perm_code 或 action+scope"""
    perm_code: str = ''

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        if u.is_super_admin:
            return True
        code = getattr(view, 'required_perm', None) or self.perm_code
        return has_permission(u, code) if code else True


def perm_class(code: str):
    """工厂：动态创建 Permission 类"""
    return type(f'Perm_{code}', (RequirePerm,), {'perm_code': code})


class IsSuperAdmin(BasePermission):
    """仅超级管理员（哈希链校验等系统级操作）"""
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return u.is_super_admin


class CanManageUsers(BasePermission):
    """可以管理用户（RBAC：user:manage_users:* 任一 scope）"""
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        if u.is_super_admin:
            return True
        return (has_permission(u, 'user:manage_users:all')
                or has_permission(u, 'user:manage_users:department')
                or has_permission(u, 'user:manage_users:team'))


class CanReadAudit(BasePermission):
    """审计日志读取（任何人都可查自己权限范围内的）"""
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return True


class RequireKnowledgePerm(BasePermission):
    """知识库操作权限：需要指定 action + scope"""
    default_action = 'read'
    default_scope = 'team'

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        action = getattr(view, 'knowledge_action', self.default_action)
        scope = getattr(view, 'knowledge_scope', self.default_scope)
        return has_perm_for_scope(u, action, scope)


class IsAdminOrOps(BasePermission):
    """节点 CRUD（RBAC：knowledge:manage:all）"""
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return u.is_super_admin or has_permission(u, 'knowledge:manage:all')
