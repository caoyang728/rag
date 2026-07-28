"""
apps.users.models - 用户与权限域

对齐数据库设计 A1~A8：
- user_account / role / permission / role_permission / user_role
- department / team / user_team

不用 Django 默认 auth_user 的原因:
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
    leader = models.ForeignKey('User', on_delete=models.SET_NULL,
                               null=True, blank=True, related_name='led_departments')
    sort_order = models.IntegerField(default=0)
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_department'
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
    leader = models.ForeignKey('User', on_delete=models.SET_NULL,
                               null=True, blank=True, related_name='led_teams')
    department = models.ForeignKey(Department, on_delete=models.SET_NULL,
                                   null=True, blank=True)
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_team'
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
# User（自定义用户模型）
# ============================================================================
class UserManager(BaseUserManager):
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


class User(AbstractBaseUser):
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

    objects = UserManager()

    class Meta:
        db_table = 'user_account'
        indexes = [
            models.Index(fields=['department']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f'{self.username}({self.real_name})'

    def save(self, *args, **kwargs):
        if self.pk:
            try:
                old = User.objects.get(pk=self.pk)
                # 超级管理员不能被禁用（使用多种方式判断，确保万无一失）
                is_sa = UserRole.objects.filter(
                    user=old, role__code='super_admin', is_active=True
                ).exists()
                if is_sa:
                    if self.status == 'disabled' and old.status != 'disabled':
                        raise ValueError("超级管理员不能被禁用")
                    if self.is_deleted and not old.is_deleted:
                        raise ValueError("超级管理员不能被删除")
            except User.DoesNotExist:
                pass
        super().save(*args, **kwargs)

    @property
    def is_active(self):
        return self.status == 'active' and not self.is_deleted

    @is_active.setter
    def is_active(self, v):
        if not v and self.pk:
            # 超级管理员不能被禁用（使用多种方式判断，确保万无一失）
            is_sa = UserRole.objects.filter(
                user=self, role__code='super_admin', is_active=True
            ).exists()
            if is_sa:
                raise ValueError("超级管理员不能被禁用")
        self.status = 'active' if v else 'disabled'

    @property
    def is_staff(self):
        """Django内置方法, 覆盖"""
        return self.is_super_admin

    @property
    def is_super_admin(self):
        """判断是否为超级管理员（使用自定义角色体系）"""
        return UserRole.objects.filter(
            user=self, role__code='super_admin', is_active=True
        ).exists()

    @property
    def is_kb_admin(self):
        """判断是否有知识库管理权限（RBAC：knowledge:manage:all）"""
        return self.is_super_admin or has_permission(self, 'knowledge:manage:all')

    @property
    def is_user_admin(self):
        """判断是否有用户管理权限（RBAC：user:manage_users:all）"""
        return self.is_super_admin or has_permission(self, 'user:manage_users:all')

    def has_perm(self, perm, obj=None):
        return self.is_super_admin or has_permission(self, perm)

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
        db_table = 'user_role_list'

    def __str__(self):
        return f'{self.code}({self.name})'


class Permission(models.Model):
    """A3: 权限项表
    code 格式: {module}:{action}:{scope}  如 knowledge:read:all
    scope: all / department / team
    """
    ACTIONS = ['read', 'upload', 'manage', 'download', 'manage_users', 'config']
    SCOPES = ['all', 'department', 'team']

    code = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=64)
    module = models.CharField(max_length=32, help_text='knowledge / user / audit / system')
    action = models.CharField(max_length=16, default='read',
                              help_text='read / upload / edit / delete / export / share / manage / restore / config')
    scope = models.CharField(max_length=16, default='team',
                             help_text='all / department / team')
    description = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'user_permission_list'
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
    granted_by = models.ForeignKey(User, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    is_active = models.BooleanField(default=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(User, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'user_role_permission_rel'
        unique_together = [('role', 'permission')]


class UserRole(models.Model):
    """A5: 用户-角色映射"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='roles')
    role = models.ForeignKey(Role, on_delete=models.CASCADE)
    granted_at = models.DateTimeField(auto_now_add=True)
    granted_by = models.ForeignKey(User, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(User, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'user_account_role_rel'
        unique_together = [('user', 'role')]
        indexes = [models.Index(fields=['role'])]


class UserTeam(models.Model):
    """A8: 用户-团队映射"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='user_teams')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='user_teams')
    role_in_team = models.CharField(max_length=32, default='member')
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'user_account_team_rel'
        unique_together = [('user', 'team')]
        indexes = [models.Index(fields=['team'])]


# ============================================================================
# 文档级权限（仅存生效数据，审计走 op_log）
# ============================================================================
class DocDenyUser(models.Model):
    """文档黑名单表 — 最高优先级拦截，物理删除"""
    doc_id = models.BigIntegerField()
    uid = models.BigIntegerField()
    create_by = models.BigIntegerField(null=True, blank=True)
    create_time = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'doc_deny_user'
        unique_together = [('doc_id', 'uid')]
        indexes = [
            models.Index(fields=['doc_id']),
            models.Index(fields=['uid']),
        ]


class DocAllowUser(models.Model):
    """个人白名单表 — 针对个人的放行权限，物理删除"""
    doc_id = models.BigIntegerField()
    uid = models.BigIntegerField()
    expire_time = models.DateTimeField(null=True, blank=True,
                                        help_text='过期时间，NULL 表示永久')
    audit_record_id = models.BigIntegerField(null=True, blank=True,
                                              help_text='关联审批工单 ID')
    create_by = models.BigIntegerField(null=True, blank=True)
    create_time = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'doc_allow_user'
        unique_together = [('doc_id', 'uid')]
        indexes = [
            models.Index(fields=['doc_id']),
            models.Index(fields=['uid']),
        ]


class DocCrossTeam(models.Model):
    """定向跨团队授权表 — 物理删除"""
    doc_id = models.BigIntegerField()
    team_code = models.CharField(max_length=64)
    expire_time = models.DateTimeField(null=True, blank=True,
                                        help_text='过期时间，NULL 表示永久')
    audit_record_id = models.BigIntegerField(null=True, blank=True,
                                              help_text='关联审批工单 ID')
    create_by = models.BigIntegerField(null=True, blank=True)
    create_time = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'doc_cross_team'
        unique_together = [('doc_id', 'team_code')]
        indexes = [
            models.Index(fields=['doc_id']),
            models.Index(fields=['team_code']),
        ]


class AccessApplication(models.Model):
    """统一权限申请单
    双轨模型：申请(拉) + 授权(推)
    target_type: doc / team / dept / all
    action: read / download
    readonly 仅可申请 read
    """
    TARGET_TYPE_CHOICES = [
        ('doc', '文档'),
        ('team', '团队'),
        ('dept', '部门'),
        ('all', '全平台'),
    ]
    ACTION_CHOICES = [
        ('read', '只读'),
        ('download', '下载'),
        ('visibility_change', '修改可见范围'),
    ]
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已批准'),
        ('rejected', '已驳回'),
        ('withdrawn', '已撤回'),
    ]

    applicant = models.ForeignKey(User, on_delete=models.CASCADE,
                                   related_name='access_applications')
    target_type = models.CharField(max_length=16, choices=TARGET_TYPE_CHOICES, default='doc')
    target_id = models.BigIntegerField(null=True, blank=True,
                                        help_text='doc_id / team_id / dept_id')
    action = models.CharField(max_length=32, choices=ACTION_CHOICES, default='read')
    reason = models.TextField(blank=True, default='', help_text='申请理由')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='pending')
    reviewer_comment = models.TextField(blank=True, default='', help_text='审批意见')
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    new_visibility = models.CharField(max_length=16, blank=True, default='',
                                       help_text='仅 visibility_change 动作使用，新可见范围: team/dept/public')
    need_double_approval = models.BooleanField(default=False,
                                                help_text='向上调整可见范围时需要双层审批')
    first_reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL,
                                           null=True, blank=True, related_name='+')
    first_reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'access_application'
        indexes = [
            models.Index(fields=['applicant']),
            models.Index(fields=['status']),
            models.Index(fields=['target_type', 'target_id']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'AccessApp<{self.applicant_id}>{self.target_type}:{self.target_id}:{self.status}'


# ============================================================================
# 辅助函数：权限判定
# ============================================================================
def has_permission(user: User, perm_code: str) -> bool:
    """
    RBAC 权限判定 —— 通过 UserRole → RolePermission → Permission 查询
    支持多角色权限并集
    """
    if user is None or not user.is_authenticated:
        return False
    return RolePermission.objects.filter(
        role__in=UserRole.objects.filter(user=user, is_active=True).values('role_id'),
        permission__code=perm_code,
        is_active=True,
    ).exists()


def has_perm_for_scope(user: User, action: str, scope: str = 'all', module: str = 'knowledge') -> bool:
    """检查用户是否拥有 {module}:{action}:{scope} 权限
    无权限继承，仅检查指定 scope 的权限
    """
    if user is None or not user.is_authenticated:
        return False

    if UserRole.objects.filter(user=user, role__code='super_admin', is_active=True).exists():
        return True

    code = f'{module}:{action}:{scope}'
    return has_permission(user, code)


def check_upload_permission(user):
    """检查用户是否有上传权限（RBAC：knowledge:upload:* 任一 scope）
    super_admin 自动放行；readonly/compliance_reviewer 等无 upload 权限的角色被拒绝"""
    if user is None or not user.is_authenticated:
        return False
    return (has_perm_for_scope(user, 'upload', 'all')
            or has_perm_for_scope(user, 'upload', 'department')
            or has_perm_for_scope(user, 'upload', 'team'))


def get_user_permission_map(user: User) -> dict:
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
