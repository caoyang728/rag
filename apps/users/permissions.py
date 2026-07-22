"""
DRF 权限类 - 基于 has_permission + scope 检查

角色体系 (8 种):
  super_admin    - 超级管理员，拥有所有权限
  kb_admin       - 知识库管理员，拥有所有文档管理权限，无人员管理
  audit_admin    - 审计管理员，审计日志查看权限
  user_admin     - 用户管理员，用户管理权限
  dept_manager   - 部门经理，管理部门成员及部门级文档
  team_leader    - 组长，管理团队成员及团队级文档
  employee       - 普通员工，个人文档CRUD+上传，部门/团队文档只读
  readonly       - 只读用户，仅文档只读权限

权限范围 (4 级): all / department / team / personal（无继承关系）
权限动作: read / upload / edit / delete / export / share / manage
"""
from rest_framework.permissions import BasePermission
from apps.users.models import has_permission, has_perm_for_scope, UserRole


class RequirePerm(BasePermission):
    """基类：子类设置 perm_code 或 action+scope"""
    perm_code: str = ''

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if UserRole.objects.filter(user=request.user, role__code='super_admin').exists():
            return True
        code = getattr(view, 'required_perm', None) or self.perm_code
        return has_permission(request.user, code) if code else True


def perm_class(code: str):
    """工厂：动态创建 Permission 类"""
    return type(f'Perm_{code}', (RequirePerm,), {'perm_code': code})


class IsSuperAdmin(BasePermission):
    """仅超级管理员"""
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return UserRole.objects.filter(user=u, role__code='super_admin').exists()


class IsAdminOrOps(BasePermission):
    """超级管理员 或 知识库管理员"""
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return UserRole.objects.filter(user=u, role__code__in=['super_admin', 'kb_admin']).exists()


class CanManageUsers(BasePermission):
    """可以管理用户：super_admin / dept_manager / team_leader / user_admin"""
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return UserRole.objects.filter(user=u, role__code__in=['super_admin', 'dept_manager', 'team_leader', 'user_admin']).exists()


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
    default_scope = 'personal'

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        action = getattr(view, 'knowledge_action', self.default_action)
        scope = getattr(view, 'knowledge_scope', self.default_scope)
        return has_perm_for_scope(u, action, scope)
