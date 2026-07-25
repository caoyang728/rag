"""users serializers - 与 apps.users.models 严格对齐"""
from rest_framework import serializers
from django.contrib.auth import get_user_model
from apps.users.models import Department, Team, Role, Permission, UserRole, UserTeam, get_user_permission_map

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
                for t in obj.team_set.all() if not t.is_deleted]


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
        return UserTeam.objects.filter(team=obj).count()


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
    class Meta:
        model = Role
        fields = ["id", "code", "name", "description", "is_builtin", "created_at"]


class PermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Permission
        fields = ["id", "code", "name", "module", "action", "scope", "description"]


class UserSerializer(serializers.ModelSerializer):
    roles = serializers.SerializerMethodField()
    department_name = serializers.CharField(source="department.name", read_only=True, default="")
    permission_map = serializers.SerializerMethodField()
    teams = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "real_name", "avatar_url", "phone",
            "department_id", "department_name", "status",
            "last_login_at", "last_login_ip",
            "created_at", "updated_at", "roles", "teams",
            "permission_map", "is_deleted",
        ]
        read_only_fields = ["last_login_at", "last_login_ip", "created_at", "updated_at", "is_deleted"]

    def get_roles(self, obj):
        return list(obj.roles.select_related("role").values("role__id", "role__code", "role__name"))

    def get_permission_map(self, obj):
        return get_user_permission_map(obj)

    def get_teams(self, obj):
        return list(UserTeam.objects.filter(user=obj).select_related("team").values(
            "team__id", "team__name", "team__code", "team__department_id", "role_in_team"
        ))


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
    role_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
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
