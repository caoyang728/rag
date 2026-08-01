"""
apps.users.models - 用户与权限域（RBAC + 属地授权）

权限体系三层解耦：
1. 人事组织（Dept/Team/User）：静态档案，决定员工归属
2. 角色模板（Role）：功能权限点集合，只定义"能做什么"，全局复用
3. 数据管辖 Scope（UserRoleRel/UserDeptScopeRel/UserTeamScopeRel）：动态绑定，决定"能管哪些资源"

核心模型清单：
- Department / Team：组织架构（树形部门 + 团队隶属单一部门）
- User：员工（单主部门 / 单所属团队）
- Role / Permission / RolePermissionRel：角色与权限点
- UserRoleRel / UserDeptScopeRel / UserTeamScopeRel：三类授权绑定（全局/部门属地/团队属地）
- PermissionApprovalTicket：权限变更审批工单
- PermissionAuditLog：统一审计日志（全域、永不删）

权限判定铁律：
- 代码只判断 permission_key，永不判断 role_key（super_admin 作为系统级快路径例外）
- 功能权限取并集；数据权限取最高范围等级
- Deny Override：黑名单优先级最高，在所有白名单之前判定（见 knowledge.access）
"""
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


# ============================================================================
# 枚举定义（统一管理，避免散落字符串）
# ============================================================================

class RoleType(models.TextChoices):
    """角色类型 —— 决定授权时是否需要绑定管辖范围（Scope）"""
    GLOBAL = 'GLOBAL', _('全局角色（授权无需绑定 Scope）')
    DEPT_SCOPE = 'DEPT_SCOPE', _('部门管理角色（授权必须绑定具体 Dept）')
    TEAM_SCOPE = 'TEAM_SCOPE', _('团队管理角色（授权必须绑定具体 Team）')
    NORMAL_USER = 'NORMAL_USER', _('普通角色（随人事归属生效）')


class DataScope(models.TextChoices):
    """数据权限范围等级 —— 数值越大权限越高，取最高生效"""
    TEAM = 'TEAM', _('团队级')
    DEPT = 'DEPT', _('部门级')
    GLOBAL = 'GLOBAL', _('全局级')


class GrantStatus(models.TextChoices):
    """授权状态机：PENDING(待审批) → ACTIVE(生效) → EXPIRED(过期)/REVOKED(撤销)"""
    PENDING = 'PENDING', _('待审批')
    ACTIVE = 'ACTIVE', _('生效中')
    EXPIRED = 'EXPIRED', _('已过期')
    REVOKED = 'REVOKED', _('已撤销')


class TicketStatus(models.TextChoices):
    """审批工单状态机：PENDING → APPROVED → EXECUTED；或 PENDING → REJECTED/CANCELLED"""
    PENDING = 'PENDING', _('待审批')
    APPROVED = 'APPROVED', _('已通过')
    REJECTED = 'REJECTED', _('已驳回')
    CANCELLED = 'CANCELLED', _('已撤回')
    EXECUTED = 'EXECUTED', _('已执行')


class TicketChangeType(models.TextChoices):
    """工单变更类型 —— 决定审批链路（GRANT 走完整链，REVOKE 降级可直接执行）"""
    GRANT = 'GRANT', _('授权')
    REVOKE = 'REVOKE', _('撤销')
    SCOPE_CHANGE = 'SCOPE_CHANGE', _('范围变更')
    EXPIRE_EXTEND = 'EXPIRE_EXTEND', _('延期')


class ScopeType(models.TextChoices):
    """管辖范围类型 —— 用于工单/审计记录涉及的 Scope 维度"""
    GLOBAL = 'GLOBAL', _('全局')
    DEPT = 'DEPT', _('部门')
    TEAM = 'TEAM', _('团队')
    NONE = 'NONE', _('无（全局角色）')


class AuditTargetType(models.TextChoices):
    """审计目标类型 —— 覆盖组织/节点/文档/工单/登录全域"""
    USER = 'USER', _('用户')
    ROLE = 'ROLE', _('角色')
    DEPT = 'DEPT', _('部门')
    TEAM = 'TEAM', _('团队')
    KNOWLEDGE_BASE = 'KNOWLEDGE_BASE', _('知识库')
    KNOWLEDGE_NODE = 'KNOWLEDGE_NODE', _('知识节点')
    DOCUMENT = 'DOCUMENT', _('文档')
    TICKET = 'TICKET', _('工单')
    LOGIN = 'LOGIN', _('登录')


class UserStatus(models.TextChoices):
    """用户状态 —— super_admin 不可被禁用/删除（User.save 拦截）"""
    ACTIVE = 'active', _('正常')
    DISABLED = 'disabled', _('已禁用')
    LOCKED = 'locked', _('已锁定')


# ============================================================================
# 组织架构：Department / Team
# ============================================================================

class Department(models.Model):
    """部门表（树形自关联）—— 知识节点树 Level 2 由其生命周期自动同步"""
    name = models.CharField(_('部门名称'), max_length=128)
    code = models.CharField(_('部门编码'), max_length=64, unique=True, null=True, blank=True)
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='children', help_text=_('父部门，树形结构'))
    leader = models.ForeignKey('User', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='led_departments')
    sort_order = models.IntegerField(default=0)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_department'
        verbose_name = _('部门')
        indexes = [models.Index(fields=['parent'])]
        constraints = [
            # 同级部门名不重复（仅活跃记录）
            models.UniqueConstraint(
                fields=['name'],
                condition=models.Q(is_deleted=False),
                name='unique_dept_name_active'
            )
        ]

    def __str__(self):
        return self.name


class Team(models.Model):
    """团队表 —— 隶属单一部门（dept 强制 FK），知识节点树 Level 3 由其生命周期自动同步"""
    name = models.CharField(_('团队名称'), max_length=128)
    code = models.CharField(_('团队编码'), max_length=64, unique=True, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    department = models.ForeignKey(Department, on_delete=models.PROTECT,
                                   related_name='teams', help_text=_('所属部门，强制绑定'))
    leader = models.ForeignKey('User', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='led_teams')
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_team'
        verbose_name = _('团队')
        indexes = [models.Index(fields=['department'])]
        constraints = [
            # 同部门下团队名不重复
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
    """用户管理器 —— create_superuser 自动绑定 super_admin 内置角色"""

    def create_user(self, username, email, password=None, **extra):
        if not username:
            raise ValueError('username is required')
        email = self.normalize_email(email or '')
        user = self.model(username=username, email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, email, password=None, **extra):
        """创建超级管理员 —— 自动绑定 super_admin 内置角色

        super_admin 是系统级快路径角色，鉴权时绕过所有 permission_key 判定。
        生产环境新增/撤销 super_admin 必须走双人审批工单（见 PermissionApprovalTicket）。
        """
        extra.setdefault('status', UserStatus.ACTIVE)
        user = self.create_user(username, email, password, **extra)
        super_admin_role, _ = Role.objects.get_or_create(
            role_key='super_admin',
            defaults={
                'name': '超级管理员',
                'role_type': RoleType.GLOBAL,
                'data_scope': DataScope.GLOBAL,
                'is_builtin': True,
            }
        )
        UserRoleRel.objects.get_or_create(
            user=user, role=super_admin_role,
            defaults={'status': GrantStatus.ACTIVE}
        )
        return user


class User(AbstractBaseUser):
    """系统用户主表 —— 单主部门 / 单所属团队

    注意：
    - team 为单团队 FK（员工只能归属一个团队）
    - is_super_admin 判定 role_key='super_admin'（系统级快路径）
    - is_kb_admin / is_user_admin 基于 permission_key 判定，不硬编码角色
    """
    username = models.CharField(max_length=64, unique=True)
    email = models.EmailField(max_length=128, unique=True)
    # password 字段由 AbstractBaseUser 提供
    real_name = models.CharField(max_length=64, default='')
    avatar_url = models.CharField(max_length=512, null=True, blank=True)
    phone = models.CharField(max_length=32, blank=True, default='')

    # 人事归属（单主部门 / 单所属团队）
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='users', help_text=_('主部门'))
    team = models.ForeignKey(Team, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='members', help_text=_('所属团队（单团队）'))

    status = models.CharField(max_length=16, choices=UserStatus.choices, default=UserStatus.ACTIVE)
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
            models.Index(fields=['team']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f'{self.username}({self.real_name})'

    def save(self, *args, **kwargs):
        """保存拦截 —— super_admin 不可被禁用/删除（防止误操作锁死系统）"""
        if self.pk:
            try:
                old = User.objects.get(pk=self.pk)
                # super_admin 是系统级快路径角色，禁用/删除会导致管理入口锁死
                is_sa = UserRoleRel.objects.filter(
                    user=old, role__role_key='super_admin',
                    status=GrantStatus.ACTIVE,
                ).exists()
                if is_sa:
                    if self.status == UserStatus.DISABLED and old.status != UserStatus.DISABLED:
                        raise ValueError("超级管理员不能被禁用")
                    if self.is_deleted and not old.is_deleted:
                        raise ValueError("超级管理员不能被删除")
            except User.DoesNotExist:
                pass
        super().save(*args, **kwargs)

    # ------------------------------------------------------------------
    # 状态属性
    # ------------------------------------------------------------------
    @property
    def is_active(self):
        """Django auth 兼容：status=active 且未软删"""
        return self.status == UserStatus.ACTIVE and not self.is_deleted

    @is_active.setter
    def is_active(self, v):
        if not v and self.pk:
            is_sa = UserRoleRel.objects.filter(
                user=self, role__role_key='super_admin',
                status=GrantStatus.ACTIVE,
            ).exists()
            if is_sa:
                raise ValueError("超级管理员不能被禁用")
        self.status = UserStatus.ACTIVE if v else UserStatus.DISABLED

    @property
    def is_staff(self):
        """Django admin 兼容"""
        return self.is_super_admin

    # ------------------------------------------------------------------
    # 系统级快路径判定（super_admin 绕过所有 permission_key 判定）
    # ------------------------------------------------------------------
    @property
    def is_super_admin(self):
        """是否超级管理员 —— 系统级快路径，鉴权时直接放行

        判定逻辑：检查 role_key='super_admin'，绕过所有 permission_key 判定。
        这是唯一保留 role_key 判定的地方，因为超管需要绕过所有
        permission_key 检查以避免循环查询；其他角色一律走 permission_key。
        """
        return UserRoleRel.objects.filter(
            user=self, role__role_key='super_admin',
            status=GrantStatus.ACTIVE,
        ).exists()

    @property
    def is_kb_admin(self):
        """是否有知识库管理权限 —— 基于 permission_key 判定（清除角色硬编码）"""
        return self.is_super_admin or has_permission(self, 'kb.manage_all')

    @property
    def is_user_admin(self):
        """是否有用户管理权限 —— 基于 permission_key 判定"""
        return self.is_super_admin or has_permission(self, 'user.manage_all')

    # ------------------------------------------------------------------
    # Django auth 兼容方法
    # ------------------------------------------------------------------
    def has_perm(self, perm, obj=None):
        return self.is_super_admin or has_permission(self, perm)

    def has_module_perms(self, app_label):
        return self.is_staff


# ============================================================================
# RBAC：Role / Permission / RolePermissionRel
# ============================================================================

class Role(models.Model):
    """角色表 —— 权限点集合模板，不绑定任何组织 ID

    role_key 全局唯一：7 个内置角色（is_builtin=True）系统启动时种子写入，不可删除。
    自定义角色（未来扩展）可软删。
    """
    role_key = models.CharField(_('角色标识'), max_length=64, unique=True,
                                help_text=_('全局唯一，如 super_admin / team_leader'))
    name = models.CharField(_('角色名称'), max_length=64)
    description = models.TextField(null=True, blank=True)
    role_type = models.CharField(_('角色类型'), max_length=32, choices=RoleType.choices,
                                 default=RoleType.NORMAL_USER,
                                 help_text=_('决定授权时是否需要绑定管辖 Scope'))
    data_scope = models.CharField(_('数据范围等级'), max_length=16, choices=DataScope.choices,
                                  default=DataScope.TEAM, help_text=_('角色默认数据范围'))
    is_builtin = models.BooleanField(default=False,
                                     help_text=_('内置角色不可删除'))
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_role_list'
        verbose_name = _('角色')

    def __str__(self):
        return f'{self.role_key}({self.name})'


class Permission(models.Model):
    """权限点表 —— 三段式 permission_key = {module}.{resource}.{action}

    示例：kb.document.upload / user.invite / role.grant.global / audit.log.view
    代码只判断 permission_key，永不判断 role_key（新增角色零代码改动）。
    """
    permission_key = models.CharField(_('权限点标识'), max_length=128, unique=True,
                                      help_text=_('三段式 module.resource.action'))
    permission_name = models.CharField(_('显示名称'), max_length=64)
    module = models.CharField(_('模块'), max_length=32,
                              help_text=_('org / user / kb / system / compliance / audit'))
    is_builtin = models.BooleanField(_('是否内置'), default=False,
                                     help_text=_('内置权限点不可删除'))
    description = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'user_permission_list'
        verbose_name = _('权限点')
        indexes = [
            models.Index(fields=['module']),
            models.Index(fields=['permission_key']),
        ]

    def __str__(self):
        return self.permission_key


class RolePermissionRel(models.Model):
    """角色-权限点绑定表 —— 7 个内置角色的绑定关系为系统初始种子数据

    绑定关系跟随角色：内置角色绑定全局生效。
    软删：is_active=False + revoked_at 留痕。
    """
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='role_permissions')
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE,
                                   related_name='role_permissions')
    granted_at = models.DateTimeField(auto_now_add=True)
    granted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    is_active = models.BooleanField(default=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')

    class Meta:
        db_table = 'user_role_permission_rel'
        unique_together = [('role', 'permission')]

    def __str__(self):
        return f'{self.role.role_key} -> {self.permission.permission_key}'


# ============================================================================
# 三张授权绑定核心表（大厂标准：全局/部门属地/团队属地）
# 所有授权表统一带有效期 3 字段：effective_from / expires_at / status
# ============================================================================

class UserRoleRel(models.Model):
    """全局角色绑定表 —— 用于 4 个全局角色

    授权只需：用户 + 角色（+ 可选有效期），无需绑定 Scope。
    取消管理权 = 改 status=REVOKED，不改动人事架构。
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='user_role_rels')
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='user_role_rels')
    granted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    granted_at = models.DateTimeField(auto_now_add=True)
    effective_from = models.DateTimeField(null=True, blank=True,
                                          help_text=_('NULL = 立即生效'))
    expires_at = models.DateTimeField(null=True, blank=True,
                                      help_text=_('NULL = 永久有效'))
    status = models.CharField(max_length=16, choices=GrantStatus.choices,
                              default=GrantStatus.PENDING,
                              help_text=_('PENDING=待审批 ACTIVE=生效 EXPIRED=过期 REVOKED=撤销'))
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    ticket = models.ForeignKey('PermissionApprovalTicket', on_delete=models.SET_NULL,
                               null=True, blank=True, related_name='user_role_rels',
                               help_text=_('关联审批工单（有则填）'))

    class Meta:
        db_table = 'user_role_global_rel'
        unique_together = [('user', 'role')]
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['role', 'status']),
        ]


class UserDeptScopeRel(models.Model):
    """部门管辖绑定表 —— 给用户授予"指定某部门"的 dept_manager 权限

    关键：人在 A 部门，可以被授权管理 B 部门（跨组织代管，最小权限设计）。
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dept_scope_rels')
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='dept_scope_rels')
    dept = models.ForeignKey(Department, on_delete=models.CASCADE, related_name='scope_rels')
    granted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    granted_at = models.DateTimeField(auto_now_add=True)
    effective_from = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=GrantStatus.choices,
                              default=GrantStatus.PENDING)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    ticket = models.ForeignKey('PermissionApprovalTicket', on_delete=models.SET_NULL,
                               null=True, blank=True, related_name='dept_scope_rels')

    class Meta:
        db_table = 'user_role_dept_scope_rel'
        unique_together = [('user', 'role', 'dept')]
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['dept', 'status']),
        ]


class UserTeamScopeRel(models.Model):
    """团队管辖绑定表 —— 给用户授予"指定某团队"的 team_leader 权限

    一人多组长 = 多条记录，查询 IN(多个 team_id)。
    临时协助 = 设置 expires_at，到期自动失效。
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='team_scope_rels')
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='team_scope_rels')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='scope_rels')
    granted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    granted_at = models.DateTimeField(auto_now_add=True)
    effective_from = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=GrantStatus.choices,
                              default=GrantStatus.PENDING)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    ticket = models.ForeignKey('PermissionApprovalTicket', on_delete=models.SET_NULL,
                               null=True, blank=True, related_name='team_scope_rels')

    class Meta:
        db_table = 'user_role_team_scope_rel'
        unique_together = [('user', 'role', 'team')]
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['team', 'status']),
        ]


# ============================================================================
# 审批工单（授权变更必经流程）
# ============================================================================

class PermissionApprovalTicket(models.Model):
    """权限配置审批工单 —— 所有授权表 status=PENDING 记录，仅当工单 EXECUTED 后才改 ACTIVE

    审批规则（最终计划）：
    - 同部门授权（GRANT team_leader/employee）：团队组长单审即可
    - 跨部门/跨团队/全局角色：双轨审核（一审 + 二审）
    - super_admin 新增/撤销：强制另一个 super_admin 双人复核
    - 降级/撤销（REVOKE）：团队组长可直接执行，无需审批（但记审计）
    - 任一节点 REJECTED → 工单终态 REJECTED，不执行授权表写入
    - 审批工单永不删除，只改状态
    """
    ticket_no = models.CharField(_('工单号'), max_length=64, unique=True)
    applicant = models.ForeignKey('User', on_delete=models.CASCADE,
                                  related_name='applied_tickets',
                                  help_text=_('发起人'))
    target_user = models.ForeignKey(User, on_delete=models.CASCADE,
                                    related_name='targeted_tickets',
                                    help_text=_('被授权/被撤销对象'))
    change_type = models.CharField(max_length=16, choices=TicketChangeType.choices,
                                   help_text=_('GRANT/REVOKE/SCOPE_CHANGE/EXPIRE_EXTEND'))
    role = models.ForeignKey(Role, on_delete=models.PROTECT, null=True, blank=True,
                             related_name='tickets', help_text=_('涉及角色'))
    scope_type = models.CharField(max_length=16, choices=ScopeType.choices,
                                  default=ScopeType.NONE,
                                  help_text=_('GLOBAL/DEPT/TEAM/NONE'))
    scope_id = models.BigIntegerField(null=True, blank=True,
                                      help_text=_('dept_id 或 team_id（scope_type 对应）'))
    effective_from = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True, default='', help_text=_('申请理由'))

    # 审批链快照：[{approver_id, status, approved_at, comment}, ...]
    # 顺序执行，前一节点通过才到下一节点（不支持会签并行）
    approval_chain = models.JSONField(default=list, blank=True,
                                      help_text=_('审批链快照，顺序执行'))
    current_step = models.IntegerField(default=0, help_text=_('当前审批节点索引'))

    status = models.CharField(max_length=16, choices=TicketStatus.choices,
                              default=TicketStatus.PENDING)
    approved_at = models.DateTimeField(null=True, blank=True,
                                       help_text=_('最终通过时间'))
    executed_at = models.DateTimeField(null=True, blank=True,
                                       help_text=_('审批通过后真正写入授权表的时间（异步 worker 执行）'))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'permission_approval_ticket'
        verbose_name = _('权限审批工单')
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['applicant']),
            models.Index(fields=['target_user']),
            models.Index(fields=['change_type', 'status']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'Ticket<{self.ticket_no}>{self.change_type}:{self.status}'


# ============================================================================
# 统一审计日志（全域、永不删、只允许 INSERT）
# ============================================================================

class PermissionAuditLog(models.Model):
    """权限操作审计日志 —— 同步写、只追加、永不删

    覆盖 action 清单：
    - 组织架构：DEPT_CREATE/UPDATE/DELETE、TEAM_CREATE/UPDATE/DELETE、USER_INVITE/TRANSFER/LEAVE
    - 知识节点：NODE_CREATE/MOVE/RENAME/DELETE
    - 权限配置：ROLE_GRANT/REVOKE、SCOPE_GRANT/REVOKE、EXPIRE_EXTEND/EXPIRE_AUTO
    - 审批流：TICKET_CREATE/APPROVE/REJECT/CANCEL/EXECUTE
    - 资源授权：DOC/NODE_SHARE_GRANT/REVOKE/EXPIRE
    - 访问黑名单：DOC/NODE_BLOCK_ADD/REMOVE/EXPIRE
    - 登录安全：LOGIN_SUCCESS/FAIL、LOGOUT、PASSWORD_CHANGE、TOKEN_REFRESH

    合规最低保留 180 天，金融/政府类 ≥ 1 年；到期归档冷存，禁止物理删。
    """
    log_id = models.BigAutoField(primary_key=True)
    # related_name 区分于 audit.AuditLog.actor（其 related_name='audit_logs'），
    # 避免 User 模型上两个反向 accessor 同名冲突（fields.E304/E305）
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='permission_audit_logs', help_text=_('操作人'))
    action = models.CharField(max_length=32, db_index=True, help_text=_('操作类别枚举'))
    target_type = models.CharField(max_length=32, choices=AuditTargetType.choices,
                                   help_text=_('操作对象类型'))
    target_id = models.BigIntegerField(null=True, blank=True, help_text=_('对象 ID'))
    target_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+', help_text=_('若对象是人，便于检索'))
    role = models.ForeignKey(Role, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='+', help_text=_('涉及角色（有则填）'))
    scope_type = models.CharField(max_length=16, choices=ScopeType.choices, blank=True,
                                  default=ScopeType.NONE)
    scope_id = models.BigIntegerField(null=True, blank=True)
    before_snapshot = models.JSONField(null=True, blank=True,
                                       help_text=_('变更前快照（无则 null）'))
    after_snapshot = models.JSONField(null=True, blank=True,
                                      help_text=_('变更后快照（无则 null）'))
    result = models.CharField(max_length=16, default='SUCCESS',
                              help_text=_('SUCCESS / FAIL + 失败码'))
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True,
                                      help_text=_('事件时间，按此字段冷热归档'))

    class Meta:
        db_table = 'permission_audit_log'
        verbose_name = _('权限审计日志')
        indexes = [
            models.Index(fields=['action', 'created_at']),
            models.Index(fields=['actor', '-created_at']),
            models.Index(fields=['target_type', 'target_id']),
            models.Index(fields=['target_user', '-created_at']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'AuditLog<{self.log_id}>{self.action}'


# ============================================================================
# 辅助函数：权限判定与数据范围计算
# ============================================================================

def _active_grant_filter():
    """构造"有效授权"过滤条件：status=ACTIVE 且在有效期内

    effective_from NULL = 立即生效；expires_at NULL = 永久有效。
    """
    now = timezone.now()
    return (
        models.Q(status=GrantStatus.ACTIVE)
        & models.Q(models.Q(effective_from__isnull=True) | models.Q(effective_from__lte=now))
        & models.Q(models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now))
    )


def has_permission(user, permission_key: str) -> bool:
    """RBAC 权限判定 —— 复用 get_user_permissions 的 L1 缓存集合做命中判断

    功能权限取并集：用户所有有效角色（全局 + 部门属地 + 团队属地 + 默认 employee）的权限点并集。
    super_admin 由调用方通过 user.is_super_admin 快路径处理，本函数不重复判定。

    性能：通过 L1 缓存（perm:fn:{tid}:{uid}）避免每次鉴权都查 DB；
          首次未命中时由 get_user_permissions 计算并集并回填缓存，后续判定走纯内存集合查找。
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return False

    # 复用 L1 缓存集合：has_permission 与 get_user_permissions 共享同一份权限点并集
    # 避免每个权限点判定都重复查 DB（高频调用场景下节省显著开销）
    perm_set = get_user_permissions(user)
    return permission_key in perm_set


def get_user_permissions(user) -> set:
    """获取用户最终功能权限点集合（并集，去重）—— L1 缓存读写入口

    用于前端菜单/按钮渲染及 has_permission 鉴权。
    缓存策略（L1 perm:fn:{tid}:{uid}，TTL 1h）：
    - super_admin / 未登录：不走缓存（perm_cache 内部拦截）
    - 命中：直接返回反序列化后的 set
    - 未命中：查 DB 计算并集，回填缓存后返回

    缓存失效由 signals 触发：授权表变更 / 角色权限绑定变更 → invalidate_user_perms / invalidate_role_perms。
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return set()

    # 延迟导入避免循环依赖：perm_cache 内部反向引用本模块函数
    from apps.users.perm_cache import get_perm_fn, set_perm_fn

    # 1) 命中缓存直接返回（super_admin 在 get_perm_fn 内部被拦截返回 None，走下方 DB 计算）
    cached = get_perm_fn(user)
    if cached is not None:
        return cached

    # 2) 未命中：收集用户所有有效角色 ID
    #    全局角色 + 部门属地角色 + 团队属地角色 + 默认 employee 兜底
    global_role_ids = UserRoleRel.objects.filter(
        _active_grant_filter(), user=user,
    ).values_list('role_id', flat=True)
    dept_role_ids = UserDeptScopeRel.objects.filter(
        _active_grant_filter(), user=user,
    ).values_list('role_id', flat=True)
    team_role_ids = UserTeamScopeRel.objects.filter(
        _active_grant_filter(), user=user,
    ).values_list('role_id', flat=True)

    # 一次性查出兜底/限制类角色的 ID，避免逐个 Role 查询（原 3 次查询合并为 1 次）
    # 需要：employee(兜底)、read_only_employee(限制不叠加)、super_admin(高级不叠加)
    builtin_role_map = dict(
        Role.objects.filter(
            role_key__in=['employee', 'read_only_employee', 'super_admin'],
        ).values_list('role_key', 'id')
    )
    staff_role = builtin_role_map.get('employee')
    read_only_role = builtin_role_map.get('read_only_employee')
    # 高级角色（super_admin）用户不应叠加 employee 兜底权限
    # 避免超级管理员意外获得普通员工的文档读权限（与角色定位冲突）
    super_admin_role_ids = {
        rid for key, rid in builtin_role_map.items()
        if key == 'super_admin' and rid is not None
    }
    all_role_ids = set(global_role_ids) | set(dept_role_ids) | set(team_role_ids)

    # 判断是否应跳过 employee 兜底：
    # - 显式授权 read_only_employee：只读角色不应叠加下载/写权限
    # - 显式授权 super_admin：高级角色不叠加普通员工兜底
    has_read_only = read_only_role is not None and read_only_role in all_role_ids
    has_super_admin = bool(all_role_ids & super_admin_role_ids)
    if staff_role is not None and not has_read_only and not has_super_admin:
        all_role_ids.add(staff_role)

    if not all_role_ids:
        # 空集也回填缓存，避免无角色用户反复穿透 DB
        set_perm_fn(user, set())
        return set()

    perm_set = set(
        RolePermissionRel.objects.filter(
            role_id__in=all_role_ids, is_active=True,
        ).values_list('permission__permission_key', flat=True)
    )
    # 3) 回填缓存（super_admin 在 set_perm_fn 内部被拦截 no-op）
    set_perm_fn(user, perm_set)
    return perm_set


def get_user_data_scope_level(user) -> str:
    """获取用户最高数据范围等级 —— 取所有角色 data_scope 的最高值

    优先级：GLOBAL > DEPT > TEAM
    用于检索过滤：GLOBAL 级不加组织过滤（全局可见）；DEPT 级加 dept_id IN(...)；TEAM 级加 team_id IN(...)。
    配合 L4 缓存（perm:scope:level:{uid}）。
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return DataScope.TEAM

    # super_admin 直接全局级
    if user.is_super_admin:
        return DataScope.GLOBAL

    # 收集所有有效角色的 data_scope
    scopes = []
    for rel_qs in (
        UserRoleRel.objects.filter(_active_grant_filter(), user=user).select_related('role'),
        UserDeptScopeRel.objects.filter(_active_grant_filter(), user=user).select_related('role'),
        UserTeamScopeRel.objects.filter(_active_grant_filter(), user=user).select_related('role'),
    ):
        scopes.extend(rel_qs.values_list('role__data_scope', flat=True))

    # employee 兜底
    staff_scope = Role.objects.filter(role_key='employee').values_list('data_scope', flat=True).first()
    if staff_scope:
        scopes.append(staff_scope)

    if DataScope.GLOBAL in scopes:
        return DataScope.GLOBAL
    if DataScope.DEPT in scopes:
        return DataScope.DEPT
    return DataScope.TEAM


def get_user_managed_depts(user) -> set:
    """获取用户可管理部门集合（含本部门）—— 用于部门级数据过滤

    来源：UserDeptScopeRel(部门属地授权) ∪ {user.department_id}
    配合 L2 缓存（perm:scope:dept:{uid}）。
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return set()

    managed = set(
        UserDeptScopeRel.objects.filter(
            _active_grant_filter(), user=user,
    ).values_list('dept_id', flat=True)
    )
    if user.department_id:
        managed.add(user.department_id)
    return managed


def get_user_managed_teams(user) -> set:
    """获取用户可管理团队集合（含本团队）—— 用于团队级数据过滤

    来源：UserTeamScopeRel(团队属地授权) ∪ {user.team_id}
    配合 L3 缓存（perm:scope:team:{uid}）。
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return set()

    managed = set(
        UserTeamScopeRel.objects.filter(
            _active_grant_filter(), user=user,
    ).values_list('team_id', flat=True)
    )
    if user.team_id:
        managed.add(user.team_id)
    return managed


def get_user_dept_ancestors(dept_id) -> set:
    """获取部门祖先链（含自身）—— 反坑：共享给一级部门，三级部门也要能看到

    用于部门树祖先匹配，避免共享给父部门但子部门看不到的经典坑。
    """
    if not dept_id:
        return set()
    ancestors = set()
    current_id = dept_id
    visited = set()  # 防环保护
    while current_id and current_id not in visited:
        visited.add(current_id)
        ancestors.add(current_id)
        parent_id = Department.objects.filter(id=current_id).values_list('parent_id', flat=True).first()
        current_id = parent_id
    return ancestors
