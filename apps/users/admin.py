"""users admin — 注册权限相关模型到 Django Admin"""
from django.contrib import admin
from apps.users.models import (
    User, Department, Team,
    Role, Permission,
    UserRoleRel, RolePermissionRel,
    UserDeptScopeRel, UserTeamScopeRel,
    PermissionApprovalTicket, PermissionAuditLog,
)


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("id", "username", "real_name", "email", "status", "last_login_at")
    search_fields = ("username", "email", "real_name")
    list_filter = ("status", "is_deleted")


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "code", "parent")


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "code", "department")


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("id", "role_key", "name", "role_type", "data_scope", "is_builtin")
    list_filter = ("role_type", "data_scope", "is_builtin")
    search_fields = ("role_key", "name")


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ("id", "permission_key", "permission_name", "module", "is_builtin")
    list_filter = ("module", "is_builtin")
    search_fields = ("permission_key", "permission_name")


@admin.register(UserRoleRel)
class UserRoleRelAdmin(admin.ModelAdmin):
    """全局角色绑定（用户-角色授权记录）"""
    list_display = ("id", "user", "role", "status", "effective_from", "expires_at", "granted_at")
    list_filter = ("status",)
    search_fields = ("user__username", "role__role_key")


@admin.register(RolePermissionRel)
class RolePermissionRelAdmin(admin.ModelAdmin):
    """角色-权限点绑定关系"""
    list_display = ("id", "role", "permission", "is_active", "granted_at")
    list_filter = ("is_active",)


@admin.register(UserDeptScopeRel)
class UserDeptScopeRelAdmin(admin.ModelAdmin):
    """部门属地授权（跨组织代管部门）"""
    list_display = ("id", "user", "role", "dept", "status", "granted_at")
    list_filter = ("status",)
    search_fields = ("user__username", "dept__name")


@admin.register(UserTeamScopeRel)
class UserTeamScopeRelAdmin(admin.ModelAdmin):
    """团队属地授权（跨组织代管团队）"""
    list_display = ("id", "user", "role", "team", "status", "granted_at")
    list_filter = ("status",)
    search_fields = ("user__username", "team__name")


@admin.register(PermissionApprovalTicket)
class PermissionApprovalTicketAdmin(admin.ModelAdmin):
    """权限审批工单"""
    list_display = ("id", "ticket_no", "applicant", "target_user", "change_type", "status", "created_at")
    list_filter = ("status", "change_type", "scope_type")
    search_fields = ("ticket_no", "applicant__username", "target_user__username")


@admin.register(PermissionAuditLog)
class PermissionAuditLogAdmin(admin.ModelAdmin):
    """统一审计日志（只读，永不删）"""
    list_display = ("log_id", "actor", "action", "target_type", "target_id", "result", "created_at")
    list_filter = ("action", "target_type", "result")
    search_fields = ("actor__username", "target_id")
    # 审计日志只允许查看，不允许在 admin 中增删改
    readonly_fields = [f.name for f in PermissionAuditLog._meta.fields]
