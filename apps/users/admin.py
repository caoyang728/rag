"""users admin"""
from django.contrib import admin
from apps.users.models import (
    User, Department, Team, Role, Permission,
    UserRole, RolePermission,
    DocDenyUser, DocAllowUser, DocCrossTeam, AccessApplication,
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
    list_display = ("id", "code", "name", "is_builtin")


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "name", "module")
    list_filter = ("module",)


admin.site.register(UserRole)
admin.site.register(RolePermission)


@admin.register(DocDenyUser)
class DocDenyUserAdmin(admin.ModelAdmin):
    list_display = ("id", "doc_id", "uid", "create_time")
    search_fields = ("doc_id", "uid")


@admin.register(DocAllowUser)
class DocAllowUserAdmin(admin.ModelAdmin):
    list_display = ("id", "doc_id", "uid", "expire_time", "create_time")
    search_fields = ("doc_id", "uid")


@admin.register(DocCrossTeam)
class DocCrossTeamAdmin(admin.ModelAdmin):
    list_display = ("id", "doc_id", "team_code", "expire_time", "create_time")
    search_fields = ("doc_id", "team_code")


@admin.register(AccessApplication)
class AccessApplicationAdmin(admin.ModelAdmin):
    list_display = ("id", "applicant", "target_type", "target_id", "action", "status", "created_at")
    list_filter = ("status", "target_type", "action")
    search_fields = ("applicant__username", "target_id")
