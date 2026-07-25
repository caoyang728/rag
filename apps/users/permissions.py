"""
DRF 权限类 - 基于 has_permission + scope 检查

角色体系 (6 种):
  super_admin          - 超级管理员，全部配置权限、绕过双审直接发布、可物理销毁文档
  dept_manager         - 部门负责人，管辖本部门全部团队、审批扩大可见范围申请
  team_leader          - 团队组长，本团队文档一审、管理文档、调整可见范围、直接收回对外权限
  compliance_reviewer  - 文档审核员，专职合规风控、敏感内容二审校验、无日常检索问答权限
  employee             - 普通员工，检索已审核文档、上传发起双审工单、发起权限申请
  readonly             - 只读员工，检索已发布文档、发起read权限申请、禁止上传

权限范围 (3 级): all / department / team
权限动作: read / upload / manage / download / manage_users / config
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


class CanManageUsers(BasePermission):
    """可以管理用户：super_admin / dept_manager / team_leader"""
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return UserRole.objects.filter(user=u, role__code__in=['super_admin', 'dept_manager', 'team_leader']).exists()


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
    """节点 CRUD：超级管理员或知识库管理员可操作"""
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return UserRole.objects.filter(user=u, role__code__in=['super_admin', 'kb_admin']).exists()
