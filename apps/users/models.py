"""
apps.users.models - 用户与权限域

对齐数据库设计 A1~A8：
- sys_user / role / permission / role_permission / user_role
- department / team / user_team

⭐ 面试点：为什么不用 Django 默认 auth_user？
  - 字段大量扩展（真实姓名/部门/团队/密码策略/登录 IP 快照等）
  - 与自研 RBAC 深度耦合，AbstractBaseUser 更清爽
"""
import uuid
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.db import models
from django.utils import timezone


# ============================================================================
# Department / Team（组织架构）
# ============================================================================
class Department(models.Model):
    """A6: 部门表（自引用树）"""
    name = models.CharField(max_length=128)
    code = models.CharField(max_length=64, unique=True, null=True, blank=True)
    parent = models.ForeignKey('self', on_delete=models.SET_NULL,
                               null=True, blank=True, related_name='children')
    leader = models.ForeignKey('SysUser', on_delete=models.SET_NULL,
                               null=True, blank=True, related_name='led_departments')
    sort_order = models.IntegerField(default=0)
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'system_department'
        indexes = [models.Index(fields=['parent'])]
        constraints = [
            models.UniqueConstraint(
                fields=['name'],
                condition=models.Q(is_deleted=False),
                name='unique_dept_name_active'
            )
        ]

    def __str__(self):
        return self.name


class Team(models.Model):
    """A7: 团队表（跨部门虚拟组织）"""
    name = models.CharField(max_length=128)
    code = models.CharField(max_length=64, unique=True, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    leader = models.ForeignKey('SysUser', on_delete=models.SET_NULL,
                               null=True, blank=True, related_name='led_teams')
    department = models.ForeignKey(Department, on_delete=models.SET_NULL,
                                   null=True, blank=True)
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'system_team'
        indexes = [models.Index(fields=['department'])]
        constraints = [
            models.UniqueConstraint(
                fields=['name', 'department_id'],
                condition=models.Q(is_deleted=False),
                name='unique_team_name_per_dept_active'
            )
        ]

    def __str__(self):
        return self.name


# ============================================================================
# SysUser（自定义用户模型）
# ============================================================================
class SysUserManager(BaseUserManager):
    def create_user(self, username, email, password=None, **extra):
        if not username:
            raise ValueError('username is required')
        email = self.normalize_email(email or '')
        user = self.model(username=username, email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, email, password=None, **extra):
        extra.setdefault('is_active', True)
        user = self.create_user(username, email, password, **extra)
        super_admin_role, _ = Role.objects.get_or_create(
            code='super_admin',
            defaults={'name': '超级管理员', 'description': '系统超级管理员', 'is_builtin': True}
        )
        UserRole.objects.get_or_create(user=user, role=super_admin_role)
        return user


class SysUser(AbstractBaseUser):
    """A1: 系统用户主表"""
    STATUS_CHOICES = [('active', '正常'), ('disabled', '已禁用'), ('locked', '已锁定')]

    username = models.CharField(max_length=64, unique=True)
    email = models.EmailField(max_length=128, unique=True)
    # password 字段由 AbstractBaseUser 提供
    real_name = models.CharField(max_length=64, default='')
    avatar_url = models.CharField(max_length=512, null=True, blank=True)
    phone = models.CharField(max_length=32, blank=True, default='')
    department = models.ForeignKey(Department, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='users')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='active')
    last_login_at = models.DateTimeField(null=True, blank=True)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)
    password_changed_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = ['email']

    objects = SysUserManager()

    class Meta:
        db_table = 'system_user'
        indexes = [
            models.Index(fields=['department']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f'{self.username}({self.real_name})'

    def save(self, *args, **kwargs):
        if self.pk:
            try:
                old = SysUser.objects.get(pk=self.pk)
                # 超级管理员不能被禁用（使用多种方式判断，确保万无一失）
                is_sa = UserRole.objects.filter(
                    user=old, role__code='super_admin'
                ).exists()
                if is_sa:
                    if self.status == 'disabled' and old.status != 'disabled':
                        raise ValueError("超级管理员不能被禁用")
                    if self.is_deleted and not old.is_deleted:
                        raise ValueError("超级管理员不能被删除")
            except SysUser.DoesNotExist:
                pass
        super().save(*args, **kwargs)

    # ---- DRF/Admin 需要 ----
    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return self.status == 'active' and not self.is_deleted

    @is_active.setter
    def is_active(self, v):
        if not v and self.pk:
            # 超级管理员不能被禁用（使用多种方式判断，确保万无一失）
            is_sa = UserRole.objects.filter(
                user=self, role__code='super_admin'
            ).exists()
            if is_sa:
                raise ValueError("超级管理员不能被禁用")
        self.status = 'active' if v else 'disabled'

    @property
    def is_staff(self):
        return UserRole.objects.filter(
            user=self, role__code__in=['super_admin', 'kb_admin']
        ).exists()

    @property
    def is_superuser(self):
        return UserRole.objects.filter(
            user=self, role__code='super_admin'
        ).exists()

    def has_perm(self, perm, obj=None):
        return self.is_superuser or has_permission(self, perm)

    def has_module_perms(self, app_label):
        return self.is_staff


# ============================================================================
# RBAC：Role / Permission / RolePermission / UserRole
# ============================================================================
class Role(models.Model):
    """A2: 角色表"""
    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=64)
    description = models.TextField(null=True, blank=True)
    is_builtin = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'system_role_list'

    def __str__(self):
        return f'{self.code}({self.name})'


class Permission(models.Model):
    """A3: 权限项表
    code 格式: {module}:{action}:{scope}  如 knowledge:read:all
    """
    ACTIONS = ['read', 'upload', 'edit', 'delete', 'export', 'share', 'manage', 'restore', 'config']
    SCOPES = ['all', 'department', 'team', 'personal']

    code = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=64)
    module = models.CharField(max_length=32, help_text='knowledge / user / audit / system')
    action = models.CharField(max_length=16, default='read',
                              help_text='read / upload / edit / delete / export / share / manage / restore / config')
    scope = models.CharField(max_length=16, default='personal',
                             help_text='all / department / team / personal')
    description = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'system_permission_list'
        indexes = [
            models.Index(fields=['module']),
            models.Index(fields=['action']),
            models.Index(fields=['scope']),
            models.Index(fields=['module', 'action', 'scope']),
        ]

    def __str__(self):
        return self.code


class RolePermission(models.Model):
    """A4: 角色-权限映射（多对多）"""
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='role_permissions')
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE)
    granted_at = models.DateTimeField(auto_now_add=True)
    granted_by = models.ForeignKey(SysUser, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'system_role_permission_rel'
        unique_together = [('role', 'permission')]


class UserRole(models.Model):
    """A5: 用户-角色映射"""
    user = models.ForeignKey(SysUser, on_delete=models.CASCADE, related_name='roles')
    role = models.ForeignKey(Role, on_delete=models.CASCADE)
    granted_at = models.DateTimeField(auto_now_add=True)
    granted_by = models.ForeignKey(SysUser, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'system_user_role_rel'
        unique_together = [('user', 'role')]
        indexes = [models.Index(fields=['role'])]


class UserTeam(models.Model):
    """A8: 用户-团队映射"""
    user = models.ForeignKey(SysUser, on_delete=models.CASCADE, related_name='user_teams')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='user_teams')
    role_in_team = models.CharField(max_length=32, default='member')
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'system_user_team_rel'
        unique_together = [('user', 'team')]
        indexes = [models.Index(fields=['team'])]


# ============================================================================
# 文档级权限（临时授权 / 分享）
# ============================================================================
class DocumentPermission(models.Model):
    """B2+: 文档级临时授权
    支持 share 操作：对外/跨人临时授权文档
    """
    ACTION_CHOICES = [
        ('read', '只读'),
        ('edit', '编辑'),
        ('export', '导出'),
    ]

    document = models.ForeignKey('knowledge.Document', on_delete=models.CASCADE,
                                  related_name='permission_grants')
    granted_to = models.ForeignKey(SysUser, on_delete=models.CASCADE,
                                    related_name='document_grants')
    action = models.CharField(max_length=16, choices=ACTION_CHOICES, default='read')
    granted_by = models.ForeignKey(SysUser, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='+')
    granted_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True,
                                       help_text='过期时间，NULL 表示永久')

    class Meta:
        db_table = 'knowledge_document_permission_grant'
        unique_together = [('document', 'granted_to', 'action')]
        indexes = [
            models.Index(fields=['granted_to']),
            models.Index(fields=['document']),
        ]

    def __str__(self):
        return f'DocPerm<{self.document_id}>{self.action}→{self.granted_to_id}'


# ============================================================================
# 跨部门/组文档访问授权
# ============================================================================
class UserCrossScopeAccess(models.Model):
    """B3: 用户跨域访问授权
    授予用户访问非所属部门/团队的文档的权限
    例如：研发部门的用户被授予访问市场部门文档的 read 权限
    actions 存储动作列表，如 "read,upload" — 空字符串表示继承用户的 role 权限体系
    """
    user = models.ForeignKey(SysUser, on_delete=models.CASCADE,
                              related_name='cross_scope_access')
    scope_type = models.CharField(max_length=16, choices=[
        ('department', '部门'), ('team', '团队')
    ])
    scope_id = models.IntegerField(help_text='部门ID 或 团队ID')
    actions = models.CharField(max_length=255, blank=True, default='read',
                                help_text='逗号分隔的动作: read,upload,edit,delete,export,share')
    granted_by = models.ForeignKey(SysUser, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='+')
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'knowledge_user_cross_scope_grant'
        unique_together = [('user', 'scope_type', 'scope_id')]
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['scope_type', 'scope_id']),
        ]

    def __str__(self):
        return f'CrossScope<{self.user_id}>{self.scope_type}:{self.scope_id}'


# ============================================================================
# 用户本域文档操作权限（所属部门/团队）
# ============================================================================
class UserScopePermission(models.Model):
    """B4: 用户对本域（所属部门/团队）的文档操作权限
    与 RBAC 角色权限并行：角色权限决定了通用能力，此处可针对具体用户微调
    例如：某普通员工额外获得本部门的 upload 权限
    actions 存储动作列表，如 "read,upload" — 默认仅有 read
    """
    user = models.ForeignKey(SysUser, on_delete=models.CASCADE,
                              related_name='scope_permissions')
    scope_type = models.CharField(max_length=16, choices=[
        ('department', '部门'), ('team', '团队')
    ])
    scope_id = models.IntegerField(help_text='部门ID 或 团队ID')
    actions = models.CharField(max_length=255, blank=True, default='read',
                                help_text='逗号分隔: read,upload,edit,delete,export,share')
    granted_by = models.ForeignKey(SysUser, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='+')
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'knowledge_user_scope_permission'
        unique_together = [('user', 'scope_type', 'scope_id')]
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['scope_type', 'scope_id']),
        ]

    def __str__(self):
        return f'ScopePerm<{self.user_id}>{self.scope_type}:{self.scope_id}'


# ============================================================================
# 权限申请单
# ============================================================================
class PermissionApplication(models.Model):
    """权限申请单
    用户主动申请某项权限，选择审批人（团队 leader / 部门经理 / kb_ops 等）
    审批通过后由系统或审批人手动授予角色/权限。
    """
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已批准'),
        ('rejected', '已驳回'),
        ('withdrawn', '已撤回'),
    ]
    SCOPE_CHOICES = [
        ('team', '团队级'),
        ('department', '部门级'),
        ('all', '全平台'),
    ]

    applicant = models.ForeignKey(SysUser, on_delete=models.CASCADE,
                                   related_name='permission_applications')
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE,
                                    null=True, blank=True,
                                    help_text='申请的权限项，NULL 表示申请角色')
    permission_code = models.CharField(max_length=64, blank=True, default='',
                                        help_text='申请的权限 code（冗余字段，便于无 permission 时使用）')
    applied_scope = models.CharField(max_length=16, choices=SCOPE_CHOICES, default='team',
                                      help_text='申请的范围：team/department/all')
    reason = models.TextField(blank=True, default='', help_text='申请理由')
    approver = models.ForeignKey(SysUser, on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name='pending_approvals',
                                  help_text='指定的审批人')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='pending')
    reviewer_comment = models.TextField(blank=True, default='', help_text='审批意见')
    reviewed_by = models.ForeignKey(SysUser, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'system_permission_application_record'
        indexes = [
            models.Index(fields=['applicant']),
            models.Index(fields=['approver']),
            models.Index(fields=['status']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'PermApp<{self.applicant_id}>{self.permission_code or self.permission_id}:{self.status}'


# ============================================================================
# 辅助函数：权限判定
# ============================================================================
def has_permission(user: SysUser, perm_code: str) -> bool:
    """
    RBAC 权限判定 —— 通过 UserRole → RolePermission → Permission 查询
    支持多角色权限并集
    """
    if user is None or not user.is_authenticated:
        return False
    return RolePermission.objects.filter(
        role__in=UserRole.objects.filter(user=user).values('role_id'),
        permission__code=perm_code,
    ).exists()


def has_perm_for_scope(user: SysUser, action: str, scope: str = 'all', module: str = 'knowledge') -> bool:
    """检查用户是否拥有 {module}:{action}:{scope} 权限
    无权限继承，仅检查指定 scope 的权限
    """
    if user is None or not user.is_authenticated:
        return False

    if UserRole.objects.filter(user=user, role__code='super_admin').exists():
        return True

    code = f'{module}:{action}:{scope}'
    return has_permission(user, code)


def _get_owner_info(document):
    """获取文档上传者的归属信息"""
    owner = None
    owner_dept_id = None
    owner_team_ids = set()

    if document.owner_id:
        owner = SysUser.objects.filter(id=document.owner_id).first()
        if owner:
            owner_dept_id = owner.department_id
            owner_team_ids = set(
                UserTeam.objects.filter(user=owner).values_list('team_id', flat=True)
            )

    return owner, owner_dept_id, owner_team_ids


def check_document_access(user: SysUser, document, action: str) -> bool:
    """
    检查用户对指定文档是否有 action 权限

    权限判定规则：
    1. 超级管理员 → 直接放行（除了迁移他人personal文档）
    2. 文档管理员 → 全部文档权限（除了迁移他人personal文档）
    3. 文档所有者 → 全部文档权限（无论可见范围）
    4. 文档可见性匹配 + 归属链验证 → 按可见性范围授权
    5. 临时授权 → 检查document_permission

    action: read / upload / edit / delete / export / share / migrate
    """
    if user is None or not user.is_authenticated:
        return False

    # 1. 超级管理员
    if UserRole.objects.filter(user=user, role__code='super_admin').exists():
        if action == 'migrate' and document.visibility == 'personal' and document.owner_id != user.id:
            return False
        return True

    # 2. 文档管理员
    if UserRole.objects.filter(user=user, role__code='kb_admin').exists():
        if action == 'migrate' and document.visibility == 'personal' and document.owner_id != user.id:
            return False
        return True

    # 3. 文档所有者（任何级别，包括all）
    if document.owner_id == user.id:
        return True

    owner, owner_dept_id, owner_team_ids = _get_owner_info(document)

    # 4. 可见性匹配（无继承关系）
    if document.visibility == 'team':
        # 同团队成员只有read权限
        if user.user_teams.filter(team_id=document.owner_team_id).exists():
            return action == 'read'
        # 组长管理所属团队的文档
        if UserRole.objects.filter(user=user, role__code='team_leader').exists():
            leader_teams = Team.objects.filter(leader=user).values_list('id', flat=True)
            if document.owner_team_id in leader_teams:
                return True

    if document.visibility == 'department':
        # 同部门成员只有read权限
        if user.department_id and user.department_id == owner_dept_id:
            return action == 'read'
        # 部门经理管理所属部门的文档
        if UserRole.objects.filter(user=user, role__code='dept_manager').exists():
            manager_depts = Department.objects.filter(leader=user).values_list('id', flat=True)
            if owner_dept_id in manager_depts:
                return True

    if document.visibility == 'all':
        # 所有用户可读
        if action == 'read':
            return True
        # 部门经理：只能管理本部门员工上传的all文档
        if UserRole.objects.filter(user=user, role__code='dept_manager').exists():
            manager_depts = Department.objects.filter(leader=user).values_list('id', flat=True)
            if owner_dept_id in manager_depts:
                return True
        # 组长：只能管理本组员工上传的all文档
        if UserRole.objects.filter(user=user, role__code='team_leader').exists():
            leader_teams = Team.objects.filter(leader=user).values_list('id', flat=True)
            if owner_team_ids & set(leader_teams):
                return True
        return False

    if document.visibility == 'personal':
        # 其他人访问他人personal文档，只有read权限（需申请或临时授权）
        if action == 'read':
            return False
        return False

    # 5. 临时授权（跨域访问）
    if DocumentPermission.objects.filter(
        document=document,
        granted_to=user,
        action=action,
    ).filter(
        models.Q(expires_at__isnull=True) | models.Q(expires_at__gte=timezone.now())
    ).exists():
        return True
    if action == 'read':
        if DocumentPermission.objects.filter(
            document=document, granted_to=user, action__in=['edit', 'export'],
        ).filter(
            models.Q(expires_at__isnull=True) | models.Q(expires_at__gte=timezone.now())
        ).exists():
            return True

    return False


def check_upload_permission(user):
    """检查用户是否有上传权限（除readonly外都有）"""
    if user is None or not user.is_authenticated:
        return False
    if UserRole.objects.filter(user=user, role__code='readonly').exists():
        return False
    return True


def check_document_migration_permission(user, document, target_scope, target_team_id=None, target_dept_id=None):
    """
    检查用户是否有迁移文档级别的权限

    迁移规则：
    1. 只能沿归属链向下迁移：all → department → team → personal
    2. 不能跨部门/跨团队迁移
    3. 所有者不可改变
    """
    if user is None or not user.is_authenticated:
        return False

    owner, owner_dept_id, owner_team_ids = _get_owner_info(document)

    # 超级管理员和文档管理员
    if UserRole.objects.filter(user=user, role__code__in=['super_admin', 'kb_admin']).exists():
        # 不能迁移他人的personal文档
        if document.visibility == 'personal' and document.owner_id != user.id:
            return False

        # 验证迁移方向是否符合归属链
        if target_scope == 'department':
            return target_dept_id == owner_dept_id
        elif target_scope == 'team':
            return target_team_id in owner_team_ids
        elif target_scope == 'personal':
            return True
        return False

    # 文档所有者
    if document.owner_id == user.id:
        # 验证迁移方向是否符合归属链
        if target_scope == 'department':
            return target_dept_id == owner_dept_id
        elif target_scope == 'team':
            return target_team_id in owner_team_ids
        elif target_scope == 'personal':
            return True
        return False

    # 部门经理
    if document.visibility in ['department', 'all'] and UserRole.objects.filter(user=user, role__code='dept_manager').exists():
        manager_depts = Department.objects.filter(leader=user).values_list('id', flat=True)
        if owner_dept_id not in manager_depts:
            return False

        if target_scope == 'team':
            return target_team_id in owner_team_ids
        elif target_scope == 'personal':
            return True
        return False

    # 组长
    if document.visibility in ['team', 'all'] and UserRole.objects.filter(user=user, role__code='team_leader').exists():
        leader_teams = Team.objects.filter(leader=user).values_list('id', flat=True)
        if not (owner_team_ids & set(leader_teams)):
            return False

        if target_scope == 'personal':
            return True
        return False

    return False


def get_user_permission_map(user: SysUser) -> dict:
    """获取用户所有权限，按 module:action 归组"""
    if user is None or not user.is_authenticated:
        return {}
    perms = Permission.objects.filter(
        rolepermission__role__in=user.roles.values('role_id')
    ).distinct().values('code', 'module', 'action', 'scope')

    result = {}
    for p in perms:
        key = f"{p['module']}:{p['action']}"
        if key not in result:
            result[key] = []
        result[key].append(p['scope'])
    return result
