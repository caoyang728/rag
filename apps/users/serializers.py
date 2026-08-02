"""users serializers - 与 apps.users.models 严格对齐"""
import re
from rest_framework import serializers
from django.contrib.auth import get_user_model
from apps.users.models import (
    Department, Team, Role, Permission,
    UserRoleRel, get_user_permissions,
)

User = get_user_model()


class DepartmentSerializer(serializers.ModelSerializer):
    user_count = serializers.SerializerMethodField()
    teams = serializers.SerializerMethodField()
    leader_id = serializers.IntegerField(source="leader.id", read_only=True, allow_null=True, default=None)
    leader_name = serializers.SerializerMethodField()

    class Meta:
        model = Department
        fields = ["id", "name", "code", "parent_id", "leader_id", "leader_name", "sort_order", "user_count", "teams", "created_at"]

    def get_leader_name(self, obj):
        if obj.leader:
            return obj.leader.real_name or obj.leader.username
        return None

    def get_user_count(self, obj):
        # 优先读取 ViewSet 中的 annotate 值，避免 N+1
        if hasattr(obj, 'user_count'):
            return obj.user_count
        return User.objects.filter(department=obj, is_deleted=False).count()

    def get_teams(self, obj):
        # 使用 related manager 以利用 prefetch_related 优化，避免 N+1 查询
        return [{"id": t.id, "name": t.name, "code": t.code,
                 "leader_id": t.leader_id,
                 "leader_name": (t.leader.real_name or t.leader.username) if t.leader else None}
                for t in obj.teams.all() if not t.is_deleted]


class TeamSerializer(serializers.ModelSerializer):
    user_count = serializers.SerializerMethodField()
    department_name = serializers.CharField(source="department.name", read_only=True, default="")
    leader_id = serializers.IntegerField(source="leader.id", read_only=True, allow_null=True, default=None)
    leader_name = serializers.SerializerMethodField()

    class Meta:
        model = Team
        fields = ["id", "name", "code", "department_id", "department_name", "leader_id", "leader_name", "description", "user_count", "created_at"]

    def get_leader_name(self, obj):
        if obj.leader:
            return obj.leader.real_name or obj.leader.username
        return None

    def get_user_count(self, obj):
        # 单团队 FK：直接统计 User.team 指向该团队的活跃用户
        return User.objects.filter(team=obj, is_deleted=False).count()


class DepartmentWriteSerializer(serializers.ModelSerializer):
    leader_id = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = Department
        fields = ["name", "code", "parent_id", "leader_id", "sort_order"]

    def validate_leader_id(self, value):
        if value is not None and not User.objects.filter(id=value, is_deleted=False, status='active').exists():
            raise serializers.ValidationError("指定的用户不存在或已禁用")
        return value


class TeamWriteSerializer(serializers.ModelSerializer):
    leader_id = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = Team
        fields = ["name", "code", "description", "department_id", "leader_id"]

    def validate_leader_id(self, value):
        if value is not None and not User.objects.filter(id=value, is_deleted=False, status='active').exists():
            raise serializers.ValidationError("指定的用户不存在或已禁用")
        return value


class RoleSerializer(serializers.ModelSerializer):
    permission_ids = serializers.SerializerMethodField()
    # 保持 API 字段名 code 不变，内部映射到 role_key 字段
    code = serializers.CharField(source='role_key', required=True)

    class Meta:
        model = Role
        fields = ["id", "code", "name", "description", "is_builtin", "created_at", "permission_ids"]
        read_only_fields = ["is_builtin", "created_at"]

    def validate_code(self, value):
        if not re.match(r'^[a-z][a-z0-9_]*$', value):
            raise serializers.ValidationError("角色编码只能包含小写字母、数字和下划线，且以字母开头")
        if len(value) > 64:
            raise serializers.ValidationError("角色编码长度不能超过64个字符")
        return value

    def get_permission_ids(self, obj):
        if hasattr(obj, '_permission_ids_list'):
            return obj._permission_ids_list
        if hasattr(obj, '_prefetched_objects_cache') and 'role_permissions' in obj._prefetched_objects_cache:
            return [rp.permission_id for rp in obj.role_permissions.all()]
        return list(obj.role_permissions.values_list('permission_id', flat=True))


class PermissionSerializer(serializers.ModelSerializer):
    # 保持 API 字段名 code/name 不变，内部映射到 permission_key / permission_name
    # permission_key 三段式已包含完整语义，无需 action/scope 字段
    code = serializers.CharField(source='permission_key')
    name = serializers.CharField(source='permission_name')

    class Meta:
        model = Permission
        fields = ["id", "code", "name", "module", "is_builtin", "description"]
        read_only_fields = ["is_builtin"]


class UserSerializer(serializers.ModelSerializer):
    roles = serializers.SerializerMethodField()
    department_name = serializers.CharField(source="department.name", read_only=True, default="")
    permission_map = serializers.SerializerMethodField()
    team = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "real_name", "avatar_url", "phone",
            "department_id", "department_name", "status",
            "last_login_at", "last_login_ip",
            "created_at", "updated_at", "roles", "team",
            "permission_map", "is_deleted",
        ]
        read_only_fields = ["last_login_at", "last_login_ip", "created_at", "updated_at", "is_deleted"]

    def get_roles(self, obj):
        # related_name='user_role_rels'；响应字段名保持 code（前端兼容），内部取 role__role_key
        rels = [
            {"id": r["role__id"], "code": r["role__role_key"], "name": r["role__name"]}
            for r in obj.user_role_rels.select_related("role").values(
                "role__id", "role__role_key", "role__name"
            )
        ]
        codes = {r["code"] for r in rels}
        # dept_manager 存储在 UserDeptScopeRel（部门管辖绑定表），补入角色列表
        if "dept_manager" not in codes:
            dept_rel = obj.dept_scope_rels.filter(
                status="ACTIVE"
            ).select_related("role").values("role__id", "role__role_key", "role__name").first()
            if dept_rel:
                rels.append({
                    "id": dept_rel["role__id"],
                    "code": dept_rel["role__role_key"],
                    "name": dept_rel["role__name"],
                })
                codes.add("dept_manager")
        # team_leader 存储在 UserTeamScopeRel（团队管辖绑定表），补入角色列表
        if "team_leader" not in codes:
            team_rel = obj.team_scope_rels.filter(
                status="ACTIVE"
            ).select_related("role").values("role__id", "role__role_key", "role__name").first()
            if team_rel:
                rels.append({
                    "id": team_rel["role__id"],
                    "code": team_rel["role__role_key"],
                    "name": team_rel["role__name"],
                })
                codes.add("team_leader")
        # viewer 兜底展示：与 get_user_permissions 保持一致
        # 无 contributor + 无 super_admin → 补 viewer 作为人事归属的只读基础角色
        if "contributor" not in codes and "super_admin" not in codes and "viewer" not in codes:
            viewer = Role.objects.filter(role_key="viewer").values("id", "role_key", "name").first()
            if viewer:
                rels.append({"id": viewer["id"], "code": viewer["role_key"], "name": viewer["name"]})
        return rels

    def get_permission_map(self, obj):
        # 按模块分组返回 permission_key 集合，便于前端按模块渲染权限列表
        perms = get_user_permissions(obj)
        groups = {}
        for key in perms:
            module = key.split('.')[0] if '.' in key else key
            groups.setdefault(module, []).append(key)
        return groups

    def get_team(self, obj):
        # 单团队 FK（user.team）：用户人事归属最多一个团队，直接返回单对象
        if not obj.team_id:
            return None
        team = obj.team
        if not team or team.is_deleted:
            return None
        return {
            "id": team.id,
            "name": team.name,
            "code": team.code,
            "department_id": team.department_id,
        }


class UserListSerializer(serializers.ModelSerializer):
    """用户列表精简版 Serializer
    去掉列表页不展示的大字段（permission_map、avatar、phone、roles 等），减少传输和计算开销
    列表页不直接展示角色，权限详情通过单独的权限按钮弹窗查看
    """
    department_name = serializers.CharField(source="department.name", read_only=True, default="")
    team = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "real_name",
            "department_id", "department_name", "status",
            "team", "last_login_at",
        ]

    def get_team(self, obj):
        if not obj.team_id:
            return None
        team = obj.team
        if not team or team.is_deleted:
            return None
        return {
            "id": team.id,
            "name": team.name,
            "code": team.code,
            "department_id": team.department_id,
        }


class UserCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    role_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    team_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    department_id = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = User
        fields = ["username", "email", "password", "real_name", "avatar_url",
                  "department_id", "status", "role_ids", "team_ids"]


class UserUpdateSerializer(serializers.ModelSerializer):
    # role_ids 不传时保留原有角色（不传 ≠ 清空），故不设 default
    role_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    team_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    department_id = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = User
        fields = ["username", "email", "real_name", "avatar_url",
                  "department_id", "status", "role_ids", "team_ids"]

    def validate_department_id(self, value):
        if value is not None and not Department.objects.filter(id=value, is_deleted=False).exists():
            raise serializers.ValidationError("部门不存在")
        return value


class ProfileUpdateSerializer(serializers.Serializer):
    real_name = serializers.CharField(max_length=64, required=False, trim_whitespace=True)
    avatar_url = serializers.CharField(max_length=512, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True, trim_whitespace=True)
