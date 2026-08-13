"""users serializers - 与 apps.users.models 严格对齐"""
import re
from rest_framework import serializers
from django.contrib.auth import get_user_model
from apps.users.models import (
    Department, Team, Role, Permission,
    get_user_permissions,
    get_user_managed_depts, get_user_managed_teams,
    ScopeType, TicketChangeType,
)

User = get_user_model()


def _get_viewer_role():
    """获取 viewer 角色（内置角色，数据运行期不变，使用 Django cache 缓存）

    使用 cache.get_or_set 保证线程安全：同一进程内多个线程并发调用时，
    只有一个线程执行 DB 查询，其余线程等待缓存结果。
    """
    from django.core.cache import cache
    key = 'users:viewer_role_info'
    return cache.get_or_set(
        key,
        lambda: Role.objects.filter(role_key="viewer").values("id", "role_key", "name").first(),
        timeout=3600,
    )


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
        # user_count 优先读取 ViewSet Prefetch 中的 annotate 值，未预加载时回退实时统计
        return [{"id": t.id, "name": t.name, "code": t.code,
                 "leader_id": t.leader_id,
                 "leader_name": (t.leader.real_name or t.leader.username) if t.leader else None,
                 "user_count": t.user_count if hasattr(t, 'user_count')
                 else User.objects.filter(team=t, is_deleted=False).count()}
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
        # 优先读取 ViewSet 中的 annotate 值，避免 N+1
        if hasattr(obj, 'user_count'):
            return obj.user_count
        return User.objects.filter(team=obj, is_deleted=False).count()


class DepartmentWriteSerializer(serializers.ModelSerializer):
    # leader_id 只读展示:部门经理通过任命工单(GRANT dept_manager)设置,审批通过后同步
    leader_id = serializers.IntegerField(source="leader.id", read_only=True, allow_null=True, default=None)

    class Meta:
        model = Department
        fields = ["name", "code", "parent_id", "leader_id", "sort_order"]


class TeamWriteSerializer(serializers.ModelSerializer):
    # leader_id 只读展示:团队组长通过任命工单(GRANT team_leader)设置,审批通过后同步
    leader_id = serializers.IntegerField(source="leader.id", read_only=True, allow_null=True, default=None)

    class Meta:
        model = Team
        fields = ["name", "code", "description", "department_id", "leader_id"]


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
    # 管辖范围:供组织架构页对组长/部门经理的授权入口做数据过滤
    # (super_admin 不设限,前端按角色另行放行)
    managed_team_ids = serializers.SerializerMethodField()
    managed_dept_ids = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "real_name", "avatar_url", "phone",
            "department_id", "department_name", "status",
            "last_login_at", "last_login_ip",
            "created_at", "updated_at", "roles", "team",
            "permission_map", "is_deleted",
            "managed_team_ids", "managed_dept_ids",
        ]
        read_only_fields = ["last_login_at", "last_login_ip", "created_at", "updated_at", "is_deleted"]

    def get_roles(self, obj):
        # related_name='user_role_rels'；响应字段名保持 code（前端兼容），内部取 role__role_key
        # 优先使用 prefetch_related 预加载的数据（ViewSet get_queryset 已配置），
        # 避免对每个用户触发额外 DB 查询（N+1 问题）
        rels = []
        for r in obj.user_role_rels.all():
            rels.append({
                "id": r.role_id,
                "code": r.role.role_key,
                "name": r.role.name,
            })
        codes = {r["code"] for r in rels}
        # dept_manager 存储在 UserDeptScopeRel（部门管辖绑定表），补入角色列表
        # 使用 .all() + Python 过滤利用 prefetch 缓存，避免额外 DB 查询
        if "dept_manager" not in codes:
            for dr in obj.dept_scope_rels.all():
                if dr.status == "ACTIVE" and dr.role:
                    rels.append({
                        "id": dr.role_id,
                        "code": dr.role.role_key,
                        "name": dr.role.name,
                    })
                    codes.add("dept_manager")
                    break
        # team_leader 存储在 UserTeamScopeRel（团队管辖绑定表），补入角色列表
        if "team_leader" not in codes:
            for tr in obj.team_scope_rels.all():
                if tr.status == "ACTIVE" and tr.role:
                    rels.append({
                        "id": tr.role_id,
                        "code": tr.role.role_key,
                        "name": tr.role.name,
                    })
                    codes.add("team_leader")
                    break
        # viewer 兜底展示：与 get_user_permissions 保持一致
        # 无 contributor + 无 super_admin → 补 viewer 作为人事归属的只读基础角色
        # 使用模块级缓存避免每次都查询 DB（角色数据在运行期不会变化）
        if "contributor" not in codes and "super_admin" not in codes and "viewer" not in codes:
            viewer = _get_viewer_role()
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

    def get_managed_team_ids(self, obj):
        # 管辖团队集合(含本团队):组织架构页对组长/部门经理过滤"授权成员"入口
        # 复用 L3 缓存(perm:scope:team:{uid}),super_admin 前端按角色直接放行
        return sorted(get_user_managed_teams(obj))

    def get_managed_dept_ids(self, obj):
        # 管辖部门集合(属地授权部门):部门经理在本部门发起部门级/团队级授权时过滤范围
        return sorted(get_user_managed_depts(obj))


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
    # 使用 URLField 限制 avatar_url 只允许合法 URL，防止 javascript: 等恶意 URI
    avatar_url = serializers.URLField(max_length=512, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = ["username", "email", "password", "real_name", "avatar_url",
                  "department_id", "status", "role_ids", "team_ids"]


class UserUpdateSerializer(serializers.ModelSerializer):
    # role_ids 不传时保留原有角色（不传 ≠ 清空），故不设 default
    role_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    team_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    department_id = serializers.IntegerField(required=False, allow_null=True)
    # 使用 URLField 限制 avatar_url 只允许合法 URL，防止 javascript: 等恶意 URI
    avatar_url = serializers.URLField(max_length=512, required=False, allow_blank=True)

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
    avatar_url = serializers.URLField(max_length=512, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True, trim_whitespace=True)

    def validate_avatar_url(self, value):
        """限制 avatar_url 只允许 http/https 协议，防止 javascript: 等恶意 URI"""
        if value and not value.startswith(('http://', 'https://')):
            raise serializers.ValidationError("头像地址必须以 http:// 或 https:// 开头")
        return value


class AccessApplicationSerializer(serializers.Serializer):
    """权限申请 POST 请求体校验（协议层：类型/枚举/必填/参数间约束）

    业务校验（角色存在性、管理岗任命权限、资源所有者判定、previous_role 解析、
    SoD 互斥等）留在视图/服务层处理；此处只做协议层校验，
    错误文案与手写校验保持一致，避免前端契约变化。
    """
    role_key = serializers.CharField(
        required=True,
        error_messages={'required': 'role_key 必填(角色标识)'},
    )
    scope_type = serializers.ChoiceField(
        choices=[ScopeType.TEAM, ScopeType.DEPT, ScopeType.NONE, ScopeType.GLOBAL],
        default=ScopeType.NONE,
        allow_blank=True,
        error_messages={'invalid_choice': 'scope_type 取值应为 TEAM/DEPT/NONE'},
    )
    scope_id = serializers.IntegerField(
        required=False, allow_null=True,
        error_messages={'invalid': 'scope_id 应为整数'},
    )
    change_type = serializers.ChoiceField(
        choices=[TicketChangeType.GRANT, TicketChangeType.REVOKE, TicketChangeType.ROLE_CHANGE],
        default=TicketChangeType.GRANT,
        allow_blank=True,
        error_messages={'invalid_choice': 'change_type 取值应为 GRANT/REVOKE/ROLE_CHANGE'},
    )
    previous_role_id = serializers.IntegerField(
        required=False, allow_null=True,
        error_messages={'invalid': 'previous_role_id 应为整数'},
    )
    reason = serializers.CharField(
        required=True,
        allow_blank=True,
        error_messages={'required': '请填写申请理由'},
    )
    effective_from = serializers.DateTimeField(required=False, allow_null=True)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)
    # target_user_id 用 CharField：非数字字符串转 None，与旧手写 int() 强转语义一致
    # （'abc' 不会被 400 拦截，而是走业务分支兜底 403 —— 管理岗/协作角色均提示指定被授权人）
    target_user_id = serializers.CharField(
        required=False, allow_null=True, allow_blank=True,
        error_messages={'invalid': 'target_user_id 应为整数'},
    )

    def validate_scope_type(self, value):
        # 空串兜底为 NONE（兼容前端未传/传空，与手写校验行为一致）
        return value or ScopeType.NONE

    def validate_change_type(self, value):
        # 空串兜底为 GRANT，与手写校验行为一致
        return value or TicketChangeType.GRANT

    def validate_reason(self, value):
        reason = (value or '').strip()
        if not reason:
            raise serializers.ValidationError('请填写申请理由')
        return reason

    def validate_target_user_id(self, value):
        # 空值/非数字统一返回 None，由视图业务分支判定（与旧 int() 强转失败置 None 一致）
        if value in (None, ''):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def validate(self, attrs):
        # scope_id 必填校验:TEAM/DEPT 必须指定具体组织
        if attrs.get('scope_type') in (ScopeType.TEAM, ScopeType.DEPT) and not attrs.get('scope_id'):
            raise serializers.ValidationError(f"scope_type={attrs['scope_type']} 时 scope_id 必填")
        return attrs


class AssignPermissionsSerializer(serializers.Serializer):
    """角色权限分配请求体校验：permission_ids 必须为数组，元素须为合法正整数（自动去重保序）

    不传时按空数组处理（清空角色全部权限，与原手写校验语义一致）。
    逐项 int() 强转以兼容字符串数字（如 "5"），非数字/非正整数返回原错误文案。
    """
    permission_ids = serializers.ListField(
        required=False,
        default=list,
        error_messages={'not_a_list': 'permission_ids 必须是数组'},
    )

    def validate_permission_ids(self, value):
        # 逐项校验为正整数并去重保序，避免重复 ID 触发唯一约束冲突
        seen = set()
        unique = []
        for pid in value:
            try:
                pid_int = int(pid)
            except (TypeError, ValueError):
                raise serializers.ValidationError(f'无效的权限ID: {pid}')
            if pid_int <= 0:
                raise serializers.ValidationError(f'无效的权限ID: {pid}')
            if pid_int not in seen:
                seen.add(pid_int)
                unique.append(pid_int)
        return unique
