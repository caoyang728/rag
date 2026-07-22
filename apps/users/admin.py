"""users admin"""
from django.contrib import admin
from apps.users.models import SysUser, Department, Team, Role, Permission, UserRole, RolePermission


@admin.register(SysUser)
class SysUserAdmin(admin.ModelAdmin):
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
