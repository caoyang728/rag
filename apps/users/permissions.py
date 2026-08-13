"""
DRF 权限类 - 基于 RBAC（permission_key 三段式判定）

权限判定统一走 RBAC 链路：
  User → UserRoleRel/UserDeptScopeRel/UserTeamScopeRel → RolePermissionRel → Permission(permission_key)
super_admin 作为系统级快路径，在所有权限类中直接放行。

权限点格式（三段式 {module}.{resource}.{action}）：
- user.manage_all / user.manage / user.invite
- kb.manage_all / kb.node.manage / kb.document.read / kb.document.upload / kb.document.delete
- audit.log.read
- analytics.system.read / analytics.org.read
- compliance.audit

代码只判断 permission_key，永不判断 role_key（新增角色零代码改动）。
"""
from rest_framework.permissions import BasePermission
from apps.users.models import has_permission


class RequirePerm(BasePermission):
    """基类：子类设置 perm_key，或视图设置 required_perm 属性

    用法 1（子类）：class CanFoo(RequirePerm): perm_key = 'kb.document.read'
    用法 2（视图属性）：view.required_perm = 'kb.document.upload'
    用法 3（工厂）：permission_classes = [perm_class('kb.document.upload')]
    """
    perm_key: str = ''

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        # super_admin 系统级快路径
        if u.is_super_admin:
            return True
        # 优先取视图 required_perm，其次取类属性 perm_key
        key = getattr(view, 'required_perm', None) or self.perm_key
        # 安全铁律：未配置 permission_key 时默认拒绝（最小权限原则）
        # 避免视图忘记设置 required_perm 导致认证用户全部放行的越权风险
        if not key:
            return False
        return has_permission(u, key)


def perm_class(key: str):
    """工厂：动态创建 Permission 类 —— 用于视图按需指定 permission_key

    示例：permission_classes = [perm_class('kb.document.upload')]
    """
    return type(f'Perm_{key.replace(".", "_")}', (RequirePerm,), {'perm_key': key})


class IsSuperAdmin(BasePermission):
    """仅超级管理员（哈希链校验等系统级操作，不支持委派）"""
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return u.is_super_admin


class CanManageUsers(BasePermission):
    """用户管理权限 —— permission_key: user.manage_all 或 user.manage

    - user.manage_all：全局用户管理（user_admin）
    - user.manage：部门/团队内用户管理（dept_manager / team_leader）
    """
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        if u.is_super_admin:
            return True
        return (has_permission(u, 'user.manage_all')
                or has_permission(u, 'user.manage'))


class RequireKnowledgePerm(BasePermission):
    """知识库操作权限 —— 视图设置 required_perm（permission_key）

    用法：视图类设置 required_perm = 'kb.document.upload' 等权限点。
    常用权限点：kb.document.read / kb.document.upload / kb.document.delete /
               kb.node.manage / kb.manage_all
    """
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        if u.is_super_admin:
            return True
        # getattr 默认值仅在属性缺失时生效；视图显式设置 required_perm=None/空时
        # 需用 or 回退默认（与 RequirePerm 基类保持一致）
        key = getattr(view, 'required_perm', None) or 'kb.document.read'
        return has_permission(u, key)


class IsAdminOrOps(BasePermission):
    """节点 CRUD 权限 —— permission_key: kb.node.manage 或 kb.manage_all

    用于知识节点的创建/移动/重命名/删除（业务分类节点，Level 4+）。
    前 3 层（KB/部门/团队）由 node_sync.py 自动同步，不走此权限。
    """
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        if u.is_super_admin:
            return True
        return (has_permission(u, 'kb.node.manage')
                or has_permission(u, 'kb.manage_all'))


class CanViewAnalytics(BasePermission):
    """Analytics 数据查看权限

    - 系统级指标（P50/P95、队列深度、忠实度报告）：analytics.system.read
    - 组织使用报表（部门/团队维度）：analytics.org.read
    - 视图通过 required_perm 属性区分；默认 analytics.org.read
    - super_admin 始终放行
    """
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        if u.is_super_admin:
            return True
        # 同 RequireKnowledgePerm：显式 None/空时回退默认 analytics.org.read
        perm_key = getattr(view, 'required_perm', None) or 'analytics.org.read'
        return has_permission(u, perm_key)
