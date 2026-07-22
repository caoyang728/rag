"""users serializers - 与 apps.users.models 严格对齐"""
from rest_framework import serializers
from django.contrib.auth import get_user_model
from apps.users.models import Department, Team, Role, Permission, UserRole, UserTeam, UserCrossScopeAccess, UserScopePermission, get_user_permission_map

User = get_user_model()


class DepartmentSerializer(serializers.ModelSerializer):
    user_count = serializers.SerializerMethodField()
    teams = serializers.SerializerMethodField()

    class Meta:
        model = Department
        fields = ["id", "name", "code", "parent_id", "sort_order", "user_count", "teams", "created_at"]

    def get_user_count(self, obj):
        # 优先读取 ViewSet 中的 annotate 值，避免 N+1
        if hasattr(obj, 'user_count'):
            return obj.user_count
        return User.objects.filter(department=obj, is_deleted=False).count()

    def get_teams(self, obj):
        # 使用 related manager 以利用 prefetch_related 优化，避免 N+1 查询
        return [{"id": t.id, "name": t.name, "code": t.code}
                for t in obj.team_set.all() if not t.is_deleted]


class TeamSerializer(serializers.ModelSerializer):
    user_count = serializers.SerializerMethodField()
    department_name = serializers.CharField(source="department.name", read_only=True, default="")

    class Meta:
        model = Team
        fields = ["id", "name", "code", "department_id", "department_name", "description", "user_count", "created_at"]

    def get_user_count(self, obj):
        return UserTeam.objects.filter(team=obj).count()


class CrossScopeAccessSerializer(serializers.ModelSerializer):
    department_name = serializers.SerializerMethodField()
    team_name = serializers.SerializerMethodField()

    class Meta:
        model = UserCrossScopeAccess
        fields = ["id", "user_id", "scope_type", "scope_id", "actions",
                  "department_name", "team_name", "granted_at"]

    def _cached_name(self, scope_type, scope_id):
        """实例级缓存：同一 serializer 实例内相同 scope_id 不重复查询"""
        cache_key = f'_name_cache_{scope_type}'
        if not hasattr(self, cache_key):
            setattr(self, cache_key, {})
        cache = getattr(self, cache_key)
        if scope_id not in cache:
            model = Department if scope_type == 'department' else Team
            cache[scope_id] = model.objects.filter(id=scope_id).values_list('name', flat=True).first() or ''
        return cache[scope_id]

    def get_department_name(self, obj):
        if obj.scope_type == 'department':
            return self._cached_name('department', obj.scope_id)
        return ''

    def get_team_name(self, obj):
        if obj.scope_type == 'team':
            return self._cached_name('team', obj.scope_id)
        return ''


class ScopePermissionSerializer(serializers.ModelSerializer):
    department_name = serializers.SerializerMethodField()
    team_name = serializers.SerializerMethodField()

    class Meta:
        model = UserScopePermission
        fields = ["id", "user_id", "scope_type", "scope_id", "actions",
                  "department_name", "team_name", "granted_at"]

    def _cached_name(self, scope_type, scope_id):
        """实例级缓存：同一 serializer 实例内相同 scope_id 不重复查询"""
        cache_key = f'_name_cache_{scope_type}'
        if not hasattr(self, cache_key):
            setattr(self, cache_key, {})
        cache = getattr(self, cache_key)
        if scope_id not in cache:
            model = Department if scope_type == 'department' else Team
            cache[scope_id] = model.objects.filter(id=scope_id).values_list('name', flat=True).first() or ''
        return cache[scope_id]

    def get_department_name(self, obj):
        if obj.scope_type == 'department':
            return self._cached_name('department', obj.scope_id)
        return ''

    def get_team_name(self, obj):
        if obj.scope_type == 'team':
            return self._cached_name('team', obj.scope_id)
        return ''


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
    cross_scope_access = serializers.SerializerMethodField()
    scope_permissions = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "real_name", "avatar_url", "phone",
            "department_id", "department_name", "status",
            "last_login_at", "last_login_ip",
            "created_at", "updated_at", "roles", "teams",
            "cross_scope_access", "scope_permissions",
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

    def get_cross_scope_access(self, obj):
        # 使用 related manager 以利用 ViewSet 的 prefetch_related 优化
        return CrossScopeAccessSerializer(
            obj.cross_scope_access.all(), many=True
        ).data

    def get_scope_permissions(self, obj):
        # 使用 related manager 以利用 ViewSet 的 prefetch_related 优化
        return ScopePermissionSerializer(
            obj.scope_permissions.all(), many=True
        ).data


class UserCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)
    role_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    team_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    cross_scope_access = serializers.ListField(required=False, default=list)
    scope_permissions = serializers.ListField(required=False, default=list)

    class Meta:
        model = User
        fields = ["username", "email", "password", "real_name", "avatar_url",
                  "department_id", "status", "role_ids", "team_ids",
                  "cross_scope_access", "scope_permissions"]


class UserUpdateSerializer(serializers.ModelSerializer):
    role_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    team_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    cross_scope_access = serializers.ListField(required=False, default=list)
    scope_permissions = serializers.ListField(required=False, default=list)

    class Meta:
        model = User
        fields = ["username", "email", "real_name", "avatar_url",
                  "department_id", "status", "role_ids", "team_ids",
                  "cross_scope_access", "scope_permissions"]


class ProfileUpdateSerializer(serializers.Serializer):
    real_name = serializers.CharField(max_length=64, required=False, trim_whitespace=True)
    avatar_url = serializers.CharField(max_length=512, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True, trim_whitespace=True)


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField()


class ResetPasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField()
    new_password = serializers.CharField(min_length=8)
