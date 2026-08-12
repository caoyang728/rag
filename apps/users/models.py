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
- TicketList / TicketPermissionDetail：统一审批工单主表 + 权限业务详情子表（TicketFlowLog 流转日志）
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
    ROLE_CHANGE = 'ROLE_CHANGE', _('角色变更（同 scope 内升级/降级/平移，原子撤销旧角色+授予新角色）')


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


class TicketBizType(models.TextChoices):
    """工单业务类型 —— 统一工单主表的类型维度，决定详情子表与执行逻辑

    - permission：权限审批（授权/撤销/角色变更），详情在 TicketPermissionDetail
    - config：系统配置变更（含调度类配置），业务字段在主表 detail JSON
    - model：LLM 模型变更（修改/停用/删除），业务字段在主表 detail JSON
    - schedule：定时任务变更，业务字段在主表 detail JSON
    - agent：Agent 工作流人工确认（HITL），详情在 TicketAgentApprovalDetail
    - security：安全配置变更（IP 白名单/黑名单/敏感词），详情在 TicketSecurityDetail
    - org：组织架构变更（部门/团队增删改），详情在 TicketOrgDetail
    """
    PERMISSION = 'permission', _('权限审批')
    CONFIG = 'config', _('配置变更')
    MODEL = 'model', _('模型变更')
    SCHEDULE = 'schedule', _('定时任务')
    AGENT = 'agent', _('Agent审批')
    SECURITY = 'security', _('安全配置')
    ORG = 'org', _('组织变更')


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
        生产环境新增/撤销 super_admin 必须走双人审批工单（见 TicketList）。
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
    # username 全局唯一, 新用户使用相同账号会被阻止创建（规则：账号冲突不提供恢复，需改名）
    username = models.CharField(max_length=64, unique=True)
    # email 全局唯一, 冲突后由 create 接口检测是命中活跃记录（拒绝）还是已删除记录（询问恢复）
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

    @property
    def is_compliance_admin(self):
        """是否合规管理员 —— 审计视角角色，可查看全部工单（只读）"""
        return self.is_super_admin or UserRoleRel.objects.filter(
            user=self, role__role_key='compliance_admin',
            status=GrantStatus.ACTIVE,
        ).exists()

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

    role_key 全局唯一：9 个内置角色（is_builtin=True）系统启动时种子写入，不可删除。
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
    ticket = models.ForeignKey('TicketList', on_delete=models.SET_NULL,
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
    ticket = models.ForeignKey('TicketList', on_delete=models.SET_NULL,
                               null=True, blank=True, related_name='dept_scope_rels')

    class Meta:
        db_table = 'user_role_dept_scope_rel'
        # 部门级互斥 DB 兜底:同一用户在同一部门只能有一条 ACTIVE 授权记录
        # 历史撤销记录(REVOKED/EXPIRED)不受约束,保留审计轨迹
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'dept'],
                condition=models.Q(status='ACTIVE'),
                name='unique_user_dept_active',
            ),
        ]
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
    ticket = models.ForeignKey('TicketList', on_delete=models.SET_NULL,
                               null=True, blank=True, related_name='team_scope_rels')

    class Meta:
        db_table = 'user_role_team_scope_rel'
        # 团队级互斥 DB 兜底:同一用户在同一团队只能有一条 ACTIVE 授权记录
        # 保证 viewer/contributor/team_leader 在同团队内互斥(高等级覆盖低等级)
        # 历史撤销记录(REVOKED/EXPIRED)不受约束,保留审计轨迹
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'team'],
                condition=models.Q(status='ACTIVE'),
                name='unique_user_team_active',
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['team', 'status']),
        ]


# ============================================================================
# 统一工单主表（方案1：统一主表 + 业务详情子表 + 流转日志）
# ============================================================================

class TicketList(models.Model):
    """统一工单主表 —— 所有类型工单（权限/配置/模型/定时任务）的公共字段

    设计（工单中心改造，方案1）：
    - 主表只存公共字段：工单号/任务名/类型/状态/风险/发起人/审批链/时间
    - 业务差异字段按类型存各自详情子表（均 OneToOne）：
      权限 → TicketPermissionDetail；配置 → TicketConfigDetail；
      定时任务 → TicketScheduleDetail；模型 → TicketModelDetail
    - 列表查询只打主表，biz_type/status 建索引
    - 审批链（approval_chain + current_step）统一承载所有类型的流程：
      权限工单 = 共享审批池多节点链；配置/模型工单 = 审核(+超管复核) 短链
    - 流转日志 TicketFlowLog 关联主表（随工单生命周期）；
      审计日志 PermissionAuditLog 独立（只增不删，防业务对象删除丢记录）

    状态机：PENDING →(逐节点审批)→ APPROVED → EXECUTED；或 PENDING → REJECTED / CANCELLED
    审批链节点结构（JSON）：
      {approver_role, approver_scope_type, approver_scope_id,
       approver_id, status, approved_at, comment}
    """
    id = models.BigAutoField(primary_key=True)
    # 工单号 = 类型前缀 + YYYYMMDD + 4 位当日全局序列，如 QX202608080001（全局唯一）
    ticket_no = models.CharField(_('工单号'), max_length=32, unique=True)
    # 任务名：列表展示与模糊搜索用，如"给张三授予后端组贡献者"、"修改 LLM 超时配置"
    title = models.CharField(_('任务名'), max_length=128, blank=True, default='')
    biz_type = models.CharField(_('工单类型'), max_length=16, choices=TicketBizType.choices, db_index=True)
    status = models.CharField(_('状态'), max_length=16, choices=TicketStatus.choices, default=TicketStatus.PENDING, db_index=True)
    risk_level = models.CharField(_('风险等级'), max_length=8, default='normal')
    applicant = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='tickets_applied',
                                  help_text=_('发起人（权限工单=提单人；配置/模型工单=创建人）'))
    # 审批链（统一流程引擎）：节点快照 + 当前节点索引，权限工单全量使用，
    # 配置/模型工单为 1~2 节点短链（审核 + 超管复核）
    approval_chain = models.JSONField(default=list, blank=True, help_text=_('审批链快照，顺序执行'))
    current_step = models.IntegerField(default=0, help_text=_('当前审批节点索引'))

    # 业务详情在各类型 OneToOne 子表：
    # permission → TicketPermissionDetail；config → TicketConfigDetail；
    # schedule → TicketScheduleDetail；model → TicketModelDetail
    # 主表不持有任何业务字段，保证"主表=流程、子表=业务"的统一模式

    # 业务标识（可索引，config/model/schedule 按类型填充，与 system_ticket 对齐）
    operation = models.CharField(max_length=20, blank=True, default='', verbose_name=_('操作类型'))
    config_key = models.CharField(max_length=64, null=True, blank=True, db_index=True, verbose_name=_('配置项'))
    target_model_id = models.BigIntegerField(null=True, blank=True, db_index=True, verbose_name=_('目标模型ID'))

    # 时间
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name=_('创建时间'))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_('更新时间'))
    approved_at = models.DateTimeField(null=True, blank=True, verbose_name=_('审批通过时间'))
    executed_at = models.DateTimeField(null=True, blank=True, verbose_name=_('生效时间'))

    class Meta:
        db_table = 'ticket_list'
        verbose_name = _('统一工单')
        verbose_name_plural = verbose_name
        indexes = [
            models.Index(fields=['biz_type', 'status'], name='uni_biz_status_idx'),
            models.Index(fields=['applicant', 'status'], name='uni_applicant_status_idx'),
            models.Index(fields=['status', 'created_at'], name='uni_status_created_idx'),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.ticket_no} {self.get_biz_type_display()} {self.status}'

    # ------------------------------------------------------------------
    # 各类型工单业务字段代理（对应类型时有效）
    # 设计说明：主表只存公共流程字段，业务字段在各类详情子表
    # （permission→TicketPermissionDetail / config→TicketConfigDetail /
    #  schedule→TicketScheduleDetail / model→TicketModelDetail）。
    # 此处提供透传代理，让工单服务层/视图层能以 ticket.change_type / ticket.role /
    # ticket.old_value 等统一形式读取业务字段，避免服务层到处按类型解包。
    # 非对应类型返回 None/空值（子表不存在时 getattr 兜底）。
    # ------------------------------------------------------------------
    @property
    def _pd(self):
        # reverse OneToOne 不存在时抛 RelatedObjectDoesNotExist（AttributeError 子类），getattr 兜底
        return getattr(self, 'permission_detail', None)

    @property
    def _cd(self):
        # config 详情子表（biz_type=config 时有效）
        return getattr(self, 'config_detail', None)

    @property
    def _sd(self):
        # schedule 详情子表（biz_type=schedule 时有效）
        return getattr(self, 'schedule_detail', None)

    @property
    def _md(self):
        # model 详情子表（biz_type=model 时有效）
        return getattr(self, 'model_detail', None)

    @property
    def _ad(self):
        # agent 人工确认详情子表（biz_type=agent 时有效）
        return getattr(self, 'agent_approval_detail', None)

    @property
    def _od(self):
        # org 组织变更详情子表（biz_type=org 时有效）
        return getattr(self, 'org_detail', None)

    @property
    def change_type(self):
        d = self._pd
        return d.change_type if d else None

    @property
    def target_user(self):
        d = self._pd
        return d.target_user if d else None

    @property
    def target_user_id(self):
        d = self._pd
        return d.target_user_id if d else None

    @property
    def role(self):
        d = self._pd
        return d.role if d else None

    @property
    def role_id(self):
        d = self._pd
        return d.role_id if d else None

    @property
    def previous_role(self):
        d = self._pd
        return d.previous_role if d else None

    @property
    def previous_role_id(self):
        d = self._pd
        return d.previous_role_id if d else None

    @property
    def scope_type(self):
        d = self._pd
        return d.scope_type if d else None

    @property
    def scope_id(self):
        d = self._pd
        return d.scope_id if d else None

    @property
    def effective_from(self):
        d = self._pd
        return d.effective_from if d else None

    @property
    def expires_at(self):
        d = self._pd
        return d.expires_at if d else None

    @property
    def reason(self):
        # 统一 reason 代理：各类型详情子表各自存储申请/变更原因，按类型依次取
        for d in (self._pd, self._cd, self._sd, self._md, self._ad, self._od):
            if d:
                return d.reason
        return ''

    # --- config/schedule 共用字段代理（biz_type=config/schedule 时有效） ---
    @property
    def config_label(self):
        for d in (self._cd, self._sd):
            if d:
                return d.config_label
        return ''

    @property
    def old_value(self):
        for d in (self._cd, self._sd):
            if d:
                return d.old_value
        return ''

    @property
    def new_value(self):
        for d in (self._cd, self._sd):
            if d:
                return d.new_value
        return ''

    @property
    def change_summary(self):
        for d in (self._cd, self._sd):
            if d:
                return d.change_summary
        return ''

    # --- model 独有字段代理（biz_type=model 时有效） ---
    @property
    def target_model_snapshot(self):
        d = self._md
        return d.target_model_snapshot if d else {}

    @property
    def changed_fields(self):
        d = self._md
        return d.changed_fields if d else {}

    @property
    def dependency_refs(self):
        d = self._md
        return d.dependency_refs if d else []


class TicketPermissionDetail(models.Model):
    """权限审批工单详情 —— 授权/撤销/角色变更的业务字段（关联统一主表）

    与主表 TicketList 一对一：主表管流程（审批链/状态/时间），本表管业务
    （给谁授什么角色、什么范围、有效期、申请理由）。查询权限工单列表时
    可 select_related 一次性加载，避免 N+1。
    """
    ticket = models.OneToOneField(TicketList, on_delete=models.CASCADE,
                                  related_name='permission_detail',
                                  help_text=_('关联统一工单主表'))
    target_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+', help_text=_('被授权/被撤销对象'))
    change_type = models.CharField(max_length=16, choices=TicketChangeType.choices,
                                   help_text=_('GRANT/REVOKE/SCOPE_CHANGE/EXPIRE_EXTEND/ROLE_CHANGE'))
    role = models.ForeignKey(Role, on_delete=models.PROTECT, null=True, blank=True,
                             related_name='+', help_text=_('涉及角色（新角色/被撤销角色）'))
    # 仅 ROLE_CHANGE 使用：记录变更前旧角色，便于执行时撤销 + 审计回溯
    previous_role = models.ForeignKey(Role, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='+', help_text=_('角色变更工单的旧角色'))
    scope_type = models.CharField(max_length=16, choices=ScopeType.choices,
                                  default=ScopeType.NONE)
    scope_id = models.BigIntegerField(null=True, blank=True,
                                      help_text=_('dept_id 或 team_id（scope_type 对应）'))
    effective_from = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True, default='', help_text=_('申请理由'))

    class Meta:
        db_table = 'permission_ticket_detail'
        verbose_name = _('权限审批工单详情')
        indexes = [
            models.Index(fields=['target_user']),
            models.Index(fields=['change_type']),
        ]

    def __str__(self):
        return f'Detail<{self.ticket_id}> {self.change_type}'


class TicketConfigDetail(models.Model):
    """配置变更工单详情 —— 业务字段（关联统一主表，biz_type=config）

    与主表 TicketList 一对一：主表管流程（审批链/状态/时间），本表管业务
    （配置项显示名/旧值/新值/差异摘要/变更原因）。查询配置工单列表时
    可 select_related 一次性加载，避免 N+1。
    """
    ticket = models.OneToOneField(TicketList, on_delete=models.CASCADE,
                                  related_name='config_detail',
                                  help_text=_('关联统一工单主表'))
    config_label = models.CharField(max_length=128, blank=True, default='',
                                    help_text=_('配置项显示名'))
    old_value = models.TextField(blank=True, default='', help_text=_('变更前值'))
    new_value = models.TextField(blank=True, default='', help_text=_('变更后值'))
    # 多值类配置的差异摘要（JSON 字符串：{added:[...], removed:[...]}），非多值项为空
    change_summary = models.TextField(blank=True, default='', help_text=_('差异摘要(JSON 字符串)'))
    reason = models.TextField(blank=True, default='', help_text=_('变更原因'))

    class Meta:
        db_table = 'config_ticket_detail'
        verbose_name = _('配置变更工单详情')

    def __str__(self):
        return f'ConfigDetail<{self.ticket_id}> {self.config_label}'


class TicketScheduleDetail(models.Model):
    """定时任务变更工单详情 —— 业务字段（关联统一主表，biz_type=schedule）

    调度本质是 SystemConfig 的 schedule 类配置项，字段结构与配置工单一致；
    独立成表便于按业务域隔离查询与归档。
    """
    ticket = models.OneToOneField(TicketList, on_delete=models.CASCADE,
                                  related_name='schedule_detail',
                                  help_text=_('关联统一工单主表'))
    config_label = models.CharField(max_length=128, blank=True, default='',
                                    help_text=_('调度任务显示名'))
    old_value = models.TextField(blank=True, default='', help_text=_('变更前调度配置'))
    new_value = models.TextField(blank=True, default='', help_text=_('变更后调度配置'))
    change_summary = models.TextField(blank=True, default='', help_text=_('差异摘要(JSON 字符串)'))
    reason = models.TextField(blank=True, default='', help_text=_('变更原因'))

    class Meta:
        db_table = 'schedule_ticket_detail'
        verbose_name = _('定时任务工单详情')

    def __str__(self):
        return f'ScheduleDetail<{self.ticket_id}> {self.config_label}'


class TicketModelDetail(models.Model):
    """模型变更工单详情 —— 业务字段（关联统一主表，biz_type=model）

    模型变更本质是"快照 + 变更字段"：审批通过后按 changed_fields 应用新值，
    删除/停用操作据 dependency_refs 做依赖拦截，target_model_snapshot 供审计回溯。
    """
    ticket = models.OneToOneField(TicketList, on_delete=models.CASCADE,
                                  related_name='model_detail',
                                  help_text=_('关联统一工单主表'))
    target_model_snapshot = models.JSONField(default=dict, blank=True,
                                             help_text=_('创建工单时的模型快照（审计回溯用）'))
    changed_fields = models.JSONField(default=dict, blank=True,
                                      help_text=_('变更字段：{field: {old, new}}'))
    dependency_refs = models.JSONField(default=list, blank=True,
                                       help_text=_('依赖引用清单（停用/删除拦截用）'))
    reason = models.TextField(blank=True, default='', help_text=_('变更原因'))

    class Meta:
        db_table = 'model_ticket_detail'
        verbose_name = _('模型变更工单详情')

    def __str__(self):
        return f'ModelDetail<{self.ticket_id}>'


class TicketAgentApprovalDetail(models.Model):
    """Agent 工作流人工确认（HITL）工单详情 —— 关联统一主表，biz_type=agent

    与主表 TicketList 一对一：主表管流程（审批链/状态/时间），本表管业务
    （哪个工作流的哪个节点需要人工确认，确认理由）。

    reason 固定带前缀 [agent:{workflow_id}:approval]，用于审计检索与工单中心
    快速识别人工确认工单的来源；工单永不删除只改状态（与统一工单一致）。
    """
    ticket = models.OneToOneField(TicketList, on_delete=models.CASCADE,
                                  related_name='agent_approval_detail',
                                  help_text=_('关联统一工单主表'))
    workflow_id = models.BigIntegerField(db_index=True,
                                         help_text=_('Agent 工作流 ID'))
    node_id = models.CharField(max_length=64, help_text=_('待确认节点 ID'))
    reason = models.TextField(blank=True, default='',
                              help_text=_('确认理由（前缀 [agent:{wf_id}:approval]）'))

    class Meta:
        db_table = 'agent_ticket_detail'
        verbose_name = _('Agent 工作流人工确认工单详情')
        indexes = [
            models.Index(fields=['workflow_id'], name='idx_agent_detail_wf'),
        ]

    def __str__(self):
        return f'AgentApprovalDetail<{self.ticket_id}> wf={self.workflow_id}'


class SecurityConfigType(models.TextChoices):
    """安全配置类型 —— TicketSecurityDetail.security_type 枚举

    标识本次工单涉及的安全配置类别，用于审批时展示与执行时路由到对应 Service。
    """
    IP_WHITELIST = 'ip_whitelist', _('IP白名单')
    IP_BLACKLIST = 'ip_blacklist', _('IP黑名单')
    SENSITIVE_WORD = 'sensitive_word', _('敏感词')


class SecurityOperation(models.TextChoices):
    """安全配置操作类型 —— TicketSecurityDetail.operation 枚举

    标识本次工单对目标配置的操作类型，用于审批展示与执行时选择对应逻辑。
    """
    ADD = 'add', _('新增')
    EDIT = 'edit', _('编辑')
    DELETE = 'delete', _('删除')
    DISABLE = 'disable', _('禁用')


class TicketSecurityDetail(models.Model):
    """安全配置工单详情 —— 关联统一主表，biz_type=security

    与主表 TicketList 一对一：主表管流程（审批链/状态/时间），本表管业务
    （哪个安全配置、什么操作、变更前后数据）。

    风险分级策略（由调用方在创建工单时根据 security_type + operation 决定 risk_level）：
    - 低风险（直接生效）：黑名单新增、敏感词新增
    - 中风险（单审）：黑名单解封、敏感词删除/禁用
    - 高风险（双审）：白名单新增/删除/编辑
    """
    ticket = models.OneToOneField(TicketList, on_delete=models.CASCADE,
                                  related_name='security_detail',
                                  help_text=_('关联统一工单主表'))
    security_type = models.CharField(max_length=32, choices=SecurityConfigType.choices,
                                     help_text=_('安全配置类型'))
    operation = models.CharField(max_length=16, choices=SecurityOperation.choices,
                                 help_text=_('操作类型'))
    target_data = models.JSONField(help_text=_('目标数据快照（如 ip/pattern/reason/category 等）'))
    old_data = models.JSONField(null=True, blank=True,
                                help_text=_('变更前数据（编辑/删除时）'))
    new_data = models.JSONField(null=True, blank=True,
                                help_text=_('变更后数据（新增/编辑时）'))
    reason = models.TextField(blank=True, default='',
                              help_text=_('变更原因'))

    class Meta:
        db_table = 'security_ticket_detail'
        verbose_name = _('安全配置工单详情')
        indexes = [
            models.Index(fields=['security_type'], name='idx_sec_detail_type'),
        ]

    def __str__(self):
        return f'SecurityDetail<{self.ticket_id}> {self.security_type}:{self.operation}'


class OrgChangeType(models.TextChoices):
    """组织变更目标类型 —— TicketOrgDetail.org_type 枚举

    标识本次工单变更的是部门还是团队，用于审批展示与执行时路由到对应逻辑。
    """
    DEPT = 'dept', _('部门')
    TEAM = 'team', _('团队')


class OrgOperation(models.TextChoices):
    """组织变更操作类型 —— TicketOrgDetail.operation 枚举

    标识本次工单对目标组织的操作类型，用于审批展示与执行时选择对应逻辑。
    """
    ADD = 'add', _('新增')
    EDIT = 'edit', _('编辑')
    DELETE = 'delete', _('删除')


class TicketOrgDetail(models.Model):
    """组织架构变更工单详情 —— 关联统一主表，biz_type=org

    与主表 TicketList 一对一：主表管流程（审批链/状态/时间），本表管业务
    （部门还是团队、什么操作、变更前后数据快照）。

    风险分级策略（由创建方在提交时决定 risk_level）：
    - 普通（单审 USER_ADMIN）：部门/团队新增、编辑
    - 高风险（双审 USER_ADMIN + SUPER_ADMIN）：部门/团队删除（破坏性操作）

    执行时机：审批链全部通过后由 _execute_org_change 落库（Department/Team 的
    post_save 信号会自动同步知识节点树），创建工单时只做预检不落库。
    """
    ticket = models.OneToOneField(TicketList, on_delete=models.CASCADE,
                                  related_name='org_detail',
                                  help_text=_('关联统一工单主表'))
    org_type = models.CharField(max_length=16, choices=OrgChangeType.choices,
                                help_text=_('组织类型（部门/团队）'))
    operation = models.CharField(max_length=16, choices=OrgOperation.choices,
                                 help_text=_('操作类型'))
    target_data = models.JSONField(help_text=_('目标数据快照（如 id/name/code/description/department_id 等）'))
    old_data = models.JSONField(null=True, blank=True,
                                help_text=_('变更前数据（编辑/删除时）'))
    new_data = models.JSONField(null=True, blank=True,
                                help_text=_('变更后数据（新增/编辑时）'))
    reason = models.TextField(blank=True, default='',
                              help_text=_('变更原因'))

    class Meta:
        db_table = 'org_ticket_detail'
        verbose_name = _('组织变更工单详情')
        indexes = [
            models.Index(fields=['org_type'], name='idx_org_detail_type'),
        ]

    def __str__(self):
        return f'OrgDetail<{self.ticket_id}> {self.org_type}:{self.operation}'


class TicketFlowLog(models.Model):
    """工单流转日志 —— 审批时间线（关联主表，随工单生命周期）

    每条记录 = 审批链上的一个动作（提交/通过/驳回/撤回/执行），供详情页时间线渲染。
    与审计日志（PermissionAuditLog）分离的设计原因：
    - 流转日志是工单业务对象的一部分：事务内写入，失败随工单回滚，可随工单归档删除
    - 审计日志是平台级合规留痕：只增不删，写入失败不阻断主业务（审计可丢、业务不可丢）
    """
    ticket = models.ForeignKey(TicketList, on_delete=models.CASCADE,
                               related_name='flow_logs', help_text=_('关联统一工单主表'))
    # SUBMIT / APPROVE / REJECT / CANCEL / EXECUTE
    action = models.CharField(max_length=16, help_text=_('流转动作'))
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='+', help_text=_('操作人'))
    # 对应审批链第几步（0=提交动作；审批动作从 0 开始的节点索引）
    step = models.IntegerField(default=0)
    comment = models.TextField(blank=True, default='', help_text=_('审批意见'))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'ticket_flow_log'
        verbose_name = _('工单流转日志')
        indexes = [
            models.Index(fields=['ticket', 'created_at'], name='flow_log_ticket_created_idx'),
        ]
        ordering = ['created_at']

    def __str__(self):
        return f'FlowLog<{self.ticket_id}> {self.action}'


# ============================================================================
# 角色互斥规则（SoD 职责分离）
# ============================================================================

class RoleConflictRule(models.Model):
    """角色互斥规则 —— SoD（Separation of Duties）约束

    任一用户不能同时持有 role_a 和 role_b（双向，存储时仅记一条，查询时双向匹配）。
    工单创建时校验：若 target_user 已持有其中一方，申请另一方则拒绝创建工单。

    初始规则（4 高权全局角色两两互斥，共 6 条）：
    - user_admin × kb_admin
    - user_admin × compliance_admin
    - user_admin × super_admin
    - kb_admin × compliance_admin
    - kb_admin × super_admin
    - compliance_admin × super_admin

    业务背景：4 个全局高权角色都是超管拆出来的"权力分立"，
    任一用户最多只能持有其中 1 个，避免单点失控（如用户管理员 + 超管 = 自我提权）。
    """
    role_a = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='+',
                               help_text=_('互斥角色 A'))
    role_b = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='+',
                               help_text=_('互斥角色 B'))
    reason = models.CharField(max_length=128, blank=True, default='',
                              help_text=_('互斥原因（可选，便于审计回溯）'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'role_conflict_rule'
        verbose_name = _('角色互斥规则')
        unique_together = [('role_a', 'role_b')]
        indexes = [
            models.Index(fields=['role_a']),
            models.Index(fields=['role_b']),
        ]

    def __str__(self):
        return f'{self.role_a_id}×{self.role_b_id}'


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

    # 一次性查出内置角色 ID，避免逐个 Role 查询
    # viewer(默认准入只读兜底)、contributor(申请后获得的读/写/下载角色)、super_admin(高级不叠加)
    builtin_role_map = dict(
        Role.objects.filter(
            role_key__in=['viewer', 'contributor', 'super_admin'],
        ).values_list('role_key', 'id')
    )
    viewer_role = builtin_role_map.get('viewer')
    contributor_role = builtin_role_map.get('contributor')
    # super_admin 不叠加 viewer：避免超级管理员意外降权（与快路径定位冲突）
    super_admin_role_ids = {
        rid for key, rid in builtin_role_map.items()
        if key == 'super_admin' and rid is not None
    }
    all_role_ids = set(global_role_ids) | set(dept_role_ids) | set(team_role_ids)

    # 叠加兜底逻辑：
    # - 显式授权 contributor：用户已升级，不叠加 viewer，直接使用 contributor 权限（读+写+下载）
    # - 未授权 contributor + 未授权 super_admin：默认 viewer 兜底（只读）
    # - 显式授权 super_admin：不叠加任何普通兜底
    has_contributor = contributor_role is not None and contributor_role in all_role_ids
    has_super_admin = bool(all_role_ids & super_admin_role_ids)
    if not has_contributor and not has_super_admin and viewer_role is not None:
        all_role_ids.add(viewer_role)

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

    # viewer 兜底（无 contributor 时 viewer 作为基础数据范围）；
    # contributor 已显式授权时其 data_scope 已在上方 scopes 中，无需重复追加
    viewer_scope = Role.objects.filter(role_key='viewer').values_list('data_scope', flat=True).first()
    if viewer_scope:
        scopes.append(viewer_scope)

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
    """获取用户可管理/可见团队集合（含本团队）—— 用于团队级数据过滤

    来源：
    1. UserTeamScopeRel(团队属地授权，team_leader / 团队级 viewer/contributor)
    2. UserDeptScopeRel 授权部门下的所有活跃团队
       (部门级授权：dept_manager / 部门级 viewer/contributor，数据范围覆盖部门内全部团队)
    3. {user.team_id} 本团队

    配合 L3 缓存（perm:scope:team:{uid}）。
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return set()

    managed = set(
        UserTeamScopeRel.objects.filter(
            _active_grant_filter(), user=user,
    ).values_list('team_id', flat=True)
    )
    # 部门属地授权 → 该部门下所有活跃团队也纳入可见范围
    # (部门级授权人应能看到部门内其他团队的 TEAM_ONLY 文档，而非仅本团队)
    dept_ids = list(
        UserDeptScopeRel.objects.filter(
            _active_grant_filter(), user=user,
        ).values_list('dept_id', flat=True)
    )
    if dept_ids:
        managed |= set(
            Team.objects.filter(
                department_id__in=dept_ids, is_deleted=False,
            ).values_list('id', flat=True)
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
