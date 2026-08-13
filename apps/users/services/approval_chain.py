"""
apps.users.services.approval_chain - 审批人角色匹配与审批链构造

共享审批池的核心：判定用户是否具备某审批节点所需的角色（_can_approve_for_role）、
按"角色 × 场景 × 操作类型"矩阵构造审批链（build_approval_chain），
以及审批链构造所需的 Leader 缺失降级、SoD 互斥、超管配额等前置校验。
"""
from typing import Optional

from django.db.models import Q
from loguru import logger

from apps.users.models import (
    UserRoleRel, UserDeptScopeRel, UserTeamScopeRel,
    Role, Team, RoleConflictRule, GrantStatus, ScopeType, TicketChangeType,
)
from apps.users.services.ticket_base import (
    ApproverRole, ApproveStepStatus, TEAM_ROLE_KEYS, get_approved_approver_ids,
)


# ============================================================================
# 角色等级与互斥组(用于 ROLE_CHANGE 流向判定 + SoD 校验)
# ============================================================================
# 4 个全局高权角色两两互斥(4 选 1),任一用户最多持有 1 个
GLOBAL_HIGH_PRIVILEGE_KEYS = ('user_admin', 'kb_admin', 'compliance_admin', 'super_admin')


# ============================================================================
# 审批人角色匹配 —— 共享审批池的核心：判定用户是否具备某审批节点所需的角色
# ============================================================================

def _can_approve_for_role(user, approver_role: str, ticket=None) -> bool:
    """判定用户是否能审批指定 approver_role 的节点 —— 共享审批池的核心校验

    各 approver_role 对应的判定逻辑:
    - SUPER_ADMIN:用户持有 super_admin 角色(用于超管工单双审 / 全局角色复核)
    - USER_ADMIN:用户持有 user_admin 角色(用于部门经理/文档管理员/合规管理员工单审核)
        注:用户管理员 × 超管互斥,故 USER_ADMIN 与 SUPER_ADMIN 节点的候选池天然不重叠
    - TEAM_LEADER:用户是审批节点指定 scope 的团队组长(team.leader_id == user.id)
        节点带 approver_scope_id,区分"本团队组长"和"目标团队组长"
    - DEPT_LEADER:用户是审批节点指定 scope 部门的 dept_manager 角色持有人
        (基于 UserDeptScopeRel 授权,非 Department.leader_id 字段;节点带 approver_scope_id,
        区分"本部门经理"和"目标部门经理")

    排除规则(三道闸):
    1. 回避原则:申请人 / 目标用户不能审自己的工单
    2. 双人独立性:已审过该工单任一前序节点的人,不能再审后续节点
       (强制双审必须由两个不同的人完成,避免一人独审两个节点)
    3. 节点 scope 匹配:TEAM_LEADER/DEPT_LEADER 节点的 approver_scope_id 必须与
       审批人所在团队/部门匹配(避免本团队组长去审目标团队节点)
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False

    # 闸 1:回避原则 —— 申请人/目标用户不能审自己工单
    if ticket:
        if user.id == ticket.applicant_id:
            return False
        if user.id == ticket.target_user_id:
            return False

    # 闸 2:双人独立性 —— 已审过前序节点的人不能再审后续节点
    # 业务背景:双超管工单若同一人审两节点,等同单人独审,失去双审意义
    # 与系统域 SUPER_ADMIN 复核共用 get_approved_approver_ids 判定,避免逻辑漂移
    if user.id in get_approved_approver_ids(ticket):
        return False

    # 取当前节点(用于读取节点上的 approver_scope_id)
    current_node = None
    if ticket and ticket.approval_chain and ticket.current_step < len(ticket.approval_chain):
        current_node = ticket.approval_chain[ticket.current_step]

    if approver_role == ApproverRole.SUPER_ADMIN:
        # 持有 super_admin 角色即可(全局角色双审 / 兜底)
        return UserRoleRel.objects.filter(
            user=user, role__role_key='super_admin',
            status=GrantStatus.ACTIVE,
        ).exists()

    if approver_role == ApproverRole.USER_ADMIN:
        # 持有 user_admin 角色即可(部门经理/文档管理员/合规管理员工单审核)
        # 注:user_admin × super_admin 互斥,此处不会误匹配超管
        return UserRoleRel.objects.filter(
            user=user, role__role_key='user_admin',
            status=GrantStatus.ACTIVE,
        ).exists()

    if approver_role == ApproverRole.KB_ADMIN:
        # 持有 kb_admin 角色即可(部门级跨部门 viewer/contributor 授权审核)
        return UserRoleRel.objects.filter(
            user=user, role__role_key='kb_admin',
            status=GrantStatus.ACTIVE,
        ).exists()

    if approver_role == ApproverRole.TEAM_LEADER:
        # 节点必须带 approver_scope_id(本团队 or 目标团队)
        if not current_node:
            return False
        node_team_id = current_node.get('approver_scope_id')
        if not node_team_id:
            return False
        team = Team.objects.filter(id=node_team_id, is_deleted=False).only('leader_id').first()
        return bool(team and team.leader_id == user.id)

    if approver_role == ApproverRole.DEPT_LEADER:
        # 节点必须带 approver_scope_id(本部门 or 目标部门)
        if not current_node:
            return False
        node_dept_id = current_node.get('approver_scope_id')
        if not node_dept_id:
            return False
        # 部门经理身份以 dept_manager 授权为准,兼容两个授予来源(见 _get_dept_leader_id)
        if UserDeptScopeRel.objects.filter(
            dept_id=node_dept_id,
            user=user,
            role__role_key='dept_manager',
            status=GrantStatus.ACTIVE,
        ).exists():
            return True
        return UserRoleRel.objects.filter(
            user=user,
            role__role_key='dept_manager',
            status=GrantStatus.ACTIVE,
            user__department_id=node_dept_id,
        ).exists()

    return False


# ============================================================================
# 审批链构造：根据变更类型 + 范围决定走单审 / 双轨 / 直接执行
# ============================================================================

def _get_team_leader_id(team_id) -> Optional[int]:
    """获取团队组长 ID —— 单审/审核人

    组长可能为空（团队刚建立未指派），此时退化为该团队所属部门负责人审批。
    """
    if not team_id:
        return None
    team = Team.objects.filter(id=team_id).only('leader_id', 'department_id').first()
    if team and team.leader_id:
        return team.leader_id
    return None


def _get_dept_leader_id(dept_id) -> Optional[int]:
    """获取部门负责人 ID —— 从 dept_manager 角色授权中解析(复核人)

    业务背景:部门经理身份不再依赖 Department.leader_id 字段,而是以 dept_manager
    授权为准。授权存在两个来源,均需兼容:
    - UserDeptScopeRel(部门属地):工单授予的主源
    - UserRoleRel(全局) + 用户所属部门匹配:用户编辑接口 assign_roles 的历史授予路径
    若一个部门存在多条 dept_manager 授权,取先授权者。
    """
    if not dept_id:
        return None
    # 主源:部门属地授权表
    uid = UserDeptScopeRel.objects.filter(
        dept_id=dept_id,
        role__role_key='dept_manager',
        status=GrantStatus.ACTIVE,
    ).values_list('user_id', flat=True).first()
    if uid:
        return uid
    # 兼容源:全局角色表 + 用户所属部门匹配(编辑接口 assign_roles 授予)
    return UserRoleRel.objects.filter(
        role__role_key='dept_manager',
        status=GrantStatus.ACTIVE,
        user__department_id=dept_id,
    ).values_list('user_id', flat=True).first()


def _get_super_admin_ids(exclude_user_id=None, role_keys=('super_admin',)) -> list:
    """获取超级管理员 ID 列表 —— 支持按角色 key 过滤

    参数：
    - exclude_user_id：排除发起人/目标用户（不能审自己的工单）
    - role_keys：角色 key 元组，默认查 super_admin

    全局角色授权/撤销强制双人复核，不足 2 人时仍写入链路，由剩余 super_admin 审批。
    """
    sa_roles = list(Role.objects.filter(role_key__in=role_keys).values_list('id', flat=True))
    if not sa_roles:
        return []
    qs = UserRoleRel.objects.filter(
        role_id__in=sa_roles, status=GrantStatus.ACTIVE,
    ).values_list('user_id', flat=True).distinct()
    if exclude_user_id:
        qs = qs.exclude(user_id=exclude_user_id)
    return list(qs)


# ============================================================================
# Phase 1.4 辅助:SoD 互斥校验 / 超管硬约束 / Leader 缺失降级
# ============================================================================

def _check_sod_conflict(target_user, new_role) -> None:
    """SoD 互斥校验 —— 工单创建前置检查

    业务背景:4 个全局高权角色两两互斥(user_admin/kb_admin/compliance_admin/super_admin),
    任一用户最多持有其中 1 个,避免单点失控(如 user_admin + super_admin = 自我提权)。

    规则表 RoleConflictRule 双向匹配:查询 (role_a=new_role OR role_b=new_role) AND
    另一方为 target_user 当前持有的活跃角色,命中则抛错拒绝创建工单。

    仅对全局高权角色生效,组织角色(dept_manager/team_leader/contributor/viewer)不参与互斥。
    """
    if not target_user or not new_role:
        return
    if new_role.role_key not in GLOBAL_HIGH_PRIVILEGE_KEYS:
        return

    # 查 target_user 当前持有的活跃全局高权角色
    held_high_priv_keys = set(UserRoleRel.objects.filter(
        user=target_user,
        role__role_key__in=GLOBAL_HIGH_PRIVILEGE_KEYS,
        status=GrantStatus.ACTIVE,
    ).values_list('role__role_key', flat=True))
    # 申请的角色已持有 → 不是 SoD 冲突,而是 GRANT 重复(由调用方判定)
    if new_role.role_key in held_high_priv_keys:
        return

    # 查 RoleConflictRule 中 new_role 与已持有角色是否互斥
    # 双向匹配:role_a=new_role OR role_b=new_role
    conflict_qs = RoleConflictRule.objects.filter(
        Q(role_a=new_role) | Q(role_b=new_role),
    )
    for rule in conflict_qs:
        other_role = rule.role_b if rule.role_a_id == new_role.id else rule.role_a
        if other_role.role_key in held_high_priv_keys:
            raise ValueError(
                f'SoD 互斥冲突:用户 {target_user.username} 已持有 {other_role.role_key},'
                f'不能同时持有 {new_role.role_key}(规则:{rule.reason or "4 高权 4 选 1"})'
            )


def _detect_team_role_in_service(user, team_id):
    """检测用户在指定团队内已持有的团队角色 —— 服务层互斥检测(团队级)

    业务背景:同团队内团队角色(viewer/contributor/team_leader)互斥,
    高等级覆盖低等级。create_ticket 入口检测到已有旧角色时,自动将 GRANT 转为
    ROLE_CHANGE(原子撤销旧角色 + 授予新角色),避免违反 DB 唯一约束。

    返回:已持有的活跃团队角色对象,无则 None。
    """
    if not user or not team_id:
        return None
    existing_role_id = UserTeamScopeRel.objects.filter(
        user=user, team_id=team_id,
        role__role_key__in=TEAM_ROLE_KEYS,
        status=GrantStatus.ACTIVE,
    ).values_list('role_id', flat=True).first()
    if existing_role_id:
        return Role.objects.filter(id=existing_role_id, is_deleted=False).first()
    return None


def _detect_dept_role_in_service(user, dept_id):
    """检测用户在指定部门内已持有的团队角色 —— 服务层互斥检测(部门级)

    业务背景:同部门内 viewer/contributor 互斥(部门级授权写入 UserDeptScopeRel,
    该表 (user, dept) 唯一约束不含 role,同一部门同一用户只能有一条 ACTIVE 记录)。
    create_ticket 入口检测到已有旧角色时,自动将 GRANT 转为 ROLE_CHANGE,避免撞唯一约束。

    返回:已持有的活跃部门角色对象,无则 None。
    """
    if not user or not dept_id:
        return None
    existing_role_id = UserDeptScopeRel.objects.filter(
        user=user, dept_id=dept_id,
        role__role_key__in=TEAM_ROLE_KEYS,
        status=GrantStatus.ACTIVE,
    ).values_list('role_id', flat=True).first()
    if existing_role_id:
        return Role.objects.filter(id=existing_role_id, is_deleted=False).first()
    return None


def _check_super_admin_quota(applicant) -> None:
    """超管硬约束 —— super_admin 工单创建前置检查

    业务背景:超管双审要求两个不同的超管顺序审批,若可用超管 < 2 人,
    工单创建后无法流转,等于卡死。生产环境应硬约束 ≥2 超管。

    本函数在 create_ticket 入口拦截:可用超管 < 2 时拒绝创建超管工单 + 告警。
    超管数量不降级(继续降级无人能审),只能拒绝 + 提示运维补人。
    """
    # 排除申请人自己(申请人不能审自己工单)
    available_sa_ids = _get_super_admin_ids(exclude_user_id=applicant.id)
    if len(available_sa_ids) < 2:
        logger.error(
            f'[SuperAdminQuota] 可用超管不足 2 人(当前 {len(available_sa_ids)} 人),'
            f'拒绝创建超管工单。申请人={applicant.username}。请立即指派第二个超管。'
        )
        raise ValueError(
            f'可用超级管理员不足 2 人(当前 {len(available_sa_ids)} 人),'
            f'禁止创建超管工单。请先指派第二个超级管理员。'
        )


def _resolve_team_leader(team_id, exclude_user_id=None) -> tuple:
    """团队组长降级解析 —— Leader 缺失时按组织层级降级

    降级链:团队组长 → 部门经理 → 用户管理员 → 超管(超管不降级)

    返回 (approver_role, approver_scope_type, approver_scope_id):
    - 团队组长命中 → (TEAM_LEADER, TEAM, team_id)
    - 降级到部门经理 → (DEPT_LEADER, DEPT, dept_id)
    - 部门经理也缺失 → (USER_ADMIN, NONE, None)
    - 用户管理员也缺失 → (SUPER_ADMIN, NONE, None)
    - 全部缺失(开发期兜底) → (SUPER_ADMIN, NONE, None)

    exclude_user_id:排除特定用户(申请人/目标用户,避免自审),命中时降级。
    """
    if team_id:
        team = Team.objects.filter(id=team_id, is_deleted=False).only('leader_id', 'department_id').first()
        if team:
            if team.leader_id and team.leader_id != exclude_user_id:
                return (ApproverRole.TEAM_LEADER, ScopeType.TEAM, team_id)
            # 团队组长缺失或 = 申请人,降级到本部门经理
            if team.department_id:
                dept_leader_id = _get_dept_leader_id(team.department_id)
                if dept_leader_id and dept_leader_id != exclude_user_id:
                    return (ApproverRole.DEPT_LEADER, ScopeType.DEPT, team.department_id)
    # 部门经理也缺失,降级到用户管理员(全局角色)
    has_user_admin = UserRoleRel.objects.filter(
        role__role_key='user_admin', status=GrantStatus.ACTIVE,
    ).exclude(user_id=exclude_user_id or 0).exists()
    if has_user_admin:
        return (ApproverRole.USER_ADMIN, ScopeType.NONE, None)
    # 最终兜底:超管(共享审批池,任何超管可审)
    return (ApproverRole.SUPER_ADMIN, ScopeType.NONE, None)


def _resolve_dept_leader(dept_id, exclude_user_id=None) -> tuple:
    """部门经理降级解析 —— Leader 缺失时按组织层级降级

    降级链:部门经理 → 用户管理员 → 超管

    返回 (approver_role, approver_scope_type, approver_scope_id)
    """
    if dept_id:
        dept_leader_id = _get_dept_leader_id(dept_id)
        if dept_leader_id and dept_leader_id != exclude_user_id:
            return (ApproverRole.DEPT_LEADER, ScopeType.DEPT, dept_id)
    # 部门经理缺失,降级到用户管理员
    has_user_admin = UserRoleRel.objects.filter(
        role__role_key='user_admin', status=GrantStatus.ACTIVE,
    ).exclude(user_id=exclude_user_id or 0).exists()
    if has_user_admin:
        return (ApproverRole.USER_ADMIN, ScopeType.NONE, None)
    return (ApproverRole.SUPER_ADMIN, ScopeType.NONE, None)


def _build_chain_node(approver_role, scope_type=None, scope_id=None) -> dict:
    """构造审批链节点 —— 统一字段结构

    节点字段:
    - approver_role:角色定位(TEAM_LEADER/DEPT_LEADER/USER_ADMIN/SUPER_ADMIN)
    - approver_scope_type/scope_id:TEAM_LEADER/DEPT_LEADER 节点的目标组织 ID
      (区分本团队/目标团队、本部门/目标部门,共享审批池匹配用)
    - approver_id:创建时为 None,审批时回填(锁定审批人,防并发)
    - status/approved_at/comment:节点状态机字段
    """
    return {
        'approver_role': approver_role,
        'approver_scope_type': scope_type or ScopeType.NONE,
        'approver_scope_id': scope_id,
        'approver_id': None,
        'status': ApproveStepStatus.PENDING,
        'approved_at': None,
        'comment': '',
    }


def _build_super_admin_chain_2step() -> list:
    """构造双超管审批链 —— 强制两个不同超管顺序审批

    共享审批池模式:每个节点仅锁定 approver_role=SUPER_ADMIN,不锁定具体用户。
    双人独立性由 _can_approve_for_role 拦截:第一节点审者不能再审第二节点。

    使用场景:
    - 申请/撤销 user_admin 角色(用户管理员不能审自己,必须超管双审)
    - 申请/撤销 super_admin 角色(强制双人复核,避免单点授权)
    """
    return [
        _build_chain_node(ApproverRole.SUPER_ADMIN),
        _build_chain_node(ApproverRole.SUPER_ADMIN),
    ]


def _build_user_admin_then_super_chain() -> list:
    """构造"用户管理员 → 超管"双轨审批链

    使用场景:
    - 申请/撤销 部门经理/文档管理员/合规管理员(三个全局高权但非超管角色)
    - 节点1:任一用户管理员审(共享审批池)
    - 节点2:任一超管审(共享审批池,排除节点1审者 → 双人独立性)

    部署规范:应先创建 user_admin,再申请 dept_manager 等部门级角色,
    保证审批链节点1 有可用审批人,避免工单卡死。
    """
    return [
        _build_chain_node(ApproverRole.USER_ADMIN),
        _build_chain_node(ApproverRole.SUPER_ADMIN),
    ]


def build_approval_chain(applicant, target_user, change_type: str,
                         role: Role, scope_type: str, scope_id,
                         previous_role: Role = None) -> list:
    """构造审批链 —— 按"角色 × 场景 × 操作类型"矩阵决定单审/双轨/直接执行

    返回 [] 表示无需审批(REVOKE 低权角色直接执行);返回多节点表示双轨/双审。

    ============================================================
    审批规则矩阵(最终版,对齐 Phase 1 设计文档)
    ============================================================

    【GRANT 申请】
    - viewer/contributor 团队级:本团队/本部门其他团队 → 组长提单自动生效(空链)
      跨部门团队 → 资源部门经理批准
    - viewer/contributor 部门级:本部门 → 部门经理提单自动生效(空链)
      跨部门 → kb_admin 审核
    - team_leader        → 用户管理员单审(超管兜底)
    - dept_manager       → 用户管理员 → 超管 双审
    - kb_admin           → 用户管理员 → 超管 双审
    - compliance_admin   → 用户管理员 → 超管 双审
    - user_admin         → 双超管(排除申请人)
    - super_admin        → 双超管(排除申请人,强制双人独立)

    【REVOKE 撤销】(方案 B 加严)
    - viewer 跨团队       → 无需审批,直接生效 + 审计
    - contributor 任意    → 无需审批,直接生效 + 审计
    - team_leader 本部门  → 本部门经理 → 用户管理员 双审
    - team_leader 跨部门  → 本部门经理 → 目标部门经理 双审
    - dept_manager        → 用户管理员 → 超管 双审
    - kb_admin            → 用户管理员 → 超管 双审
    - compliance_admin    → 用户管理员 → 超管 双审
    - user_admin          → 双超管(排除申请人)
    - super_admin         → 双超管(排除申请人)

    【ROLE_CHANGE 角色变更】(走"新角色"的申请审批链,回避时降级)
    - 任意 → 新角色      → 按新角色的 GRANT 审批链(本团队节点回避申请人时降级)

    通用规则:
    - 双人独立性:双审工单的前序节点审批人不能再审后续节点(由 _can_approve_for_role 拦截)
    - 回避原则:申请人/目标用户不能审自己工单
    - 一票否决:任一节点 REJECTED → 工单终态 REJECTED
    - 顺序流转:前一节点未通过,后一节点不可见
    - Leader 缺失降级:团队组长→部门经理→用户管理员→超管(超管不降级)
    - 申请人无 team_id:默认跨部门,本团队节点降级到本部门经理

    :param previous_role:仅 ROLE_CHANGE 使用,记录变更前的旧角色(执行时撤销目标)
    """
    if not role:
        return []

    role_key = role.role_key
    is_sa_role = role_key == 'super_admin'

    # ── 分支 0:超管 + 用户管理员角色操作 → 强制双超管复核 ──
    # super_admin:超管角色的新增/撤销/变更需双人复核,避免单点授权风险
    # user_admin:用户管理员是首个被创建的高权角色,不能由 user_admin 自审(先有鸡先有蛋),
    #   必须由两个不同超管双审,确保超管可看到并审批首批 user_admin 申请
    # 配额校验:两者都需 ≥2 个可用超管,由 create_ticket 入口 _check_super_admin_quota 拦截
    if is_sa_role or role_key == 'user_admin':
        return _build_super_admin_chain_2step()

    # ── 分支 1:全局高权角色(kb_admin / compliance_admin) ──
    # 申请/撤销都走固定双审:USER_ADMIN → SUPER_ADMIN
    # 业务背景:这两个角色由超管拆出,需"用户管理员先审 + 超管复核"双重把关
    # 注:user_admin 已在分支 0 处理(走双超管链),不在此分支
    if role_key in ('kb_admin', 'compliance_admin'):
        return _build_user_admin_then_super_chain()

    # ── 分支 2:组织角色(dept_manager / team_leader / contributor / viewer) ──
    # 按 role_key + change_type + scope 组合判定
    if role_key == 'dept_manager':
        # 部门经理:scope_type 必为 DEPT,审批链 = 用户管理员 → 超管
        return _build_user_admin_then_super_chain()

    # team_leader / contributor / viewer 的审批链按 change_type 分发
    if change_type == TicketChangeType.REVOKE:
        return _build_revoke_chain_for_team_role(applicant, role_key, scope_type, scope_id)
    if change_type == TicketChangeType.ROLE_CHANGE:
        # 角色变更:走新角色的申请链(本团队节点回避申请人时降级)
        return _build_grant_chain_for_team_role(applicant, role_key, scope_type, scope_id,
                                                 is_role_change=True)
    # 默认 GRANT / SCOPE_CHANGE / EXPIRE_EXTEND 走申请链
    return _build_grant_chain_for_team_role(applicant, role_key, scope_type, scope_id)


def _build_grant_chain_for_team_role(applicant, role_key: str,
                                      scope_type: str, scope_id,
                                      is_role_change: bool = False) -> list:
    """构造 team_leader / contributor / viewer 的申请(GRANT)审批链

    规则矩阵(定稿,资源所有者审批):
    - viewer/contributor 团队级:本团队/本部门其他团队 → 空链(组长提单自动生效)
      跨部门团队 → 资源部门经理单审(缺失降级)
    - viewer/contributor 部门级:本部门 → 空链(部门经理提单自动生效)
      跨部门 → kb_admin 单审
    - team_leader:user_admin 单审(超管兜底) —— 部门经理发起流程,人员管理审批

    本部门判定:目标组织(团队所属部门/部门 scope)与申请人所属部门一致即视为本部门。

    is_role_change=True 时,本团队节点会触发回避(申请人自己),自动走降级链。
    """
    applicant_id = applicant.id if applicant else None
    applicant_team_id = applicant.team_id if applicant else None
    applicant_dept_id = applicant.department_id if applicant else None

    # ── viewer / contributor 申请(组长/部门经理提单:本部门自动生效,跨部门单审) ──
    # 定稿矩阵(资源所有者审批):
    # - 团队级:本团队 / 本部门其他团队 → 资源团队组长提单自动生效(空链直接执行)
    #   跨部门团队 → 资源团队组长提单 + 资源部门经理批准
    # - 部门级:本部门 → 部门经理提单自动生效;跨部门 → 资源部门经理提单 + kb_admin 审核
    # 本部门判定:目标组织(团队所属部门/部门 scope)与申请人所属部门一致即视为本部门,
    # 同部门内由资源组长/经理背书即可,不再上双重审批(避免小权限大流程倒挂)。
    if role_key in ('viewer', 'contributor'):
        # 部门级授权(scope_id = dept_id)
        if scope_type == ScopeType.DEPT:
            if not scope_id:
                # 部门 scope 缺少 dept_id,异常情况降级到超管兜底
                return [_build_chain_node(ApproverRole.SUPER_ADMIN)]
            if applicant_dept_id and scope_id == applicant_dept_id:
                # 本部门:部门经理提单自动生效
                return []
            # 跨部门:资源部门经理提单 + kb_admin 审核
            return [_build_chain_node(ApproverRole.KB_ADMIN)]
        # 团队级授权(scope_id = team_id)
        if scope_type != ScopeType.TEAM or not scope_id:
            return [_build_chain_node(ApproverRole.SUPER_ADMIN)]
        target_team = Team.objects.filter(id=scope_id, is_deleted=False).only('department_id').first()
        if not target_team or not target_team.department_id:
            # 目标团队无部门归属,无法判定本部门/跨部门,兜底超管单审
            return [_build_chain_node(ApproverRole.SUPER_ADMIN)]
        target_dept_id = target_team.department_id
        if scope_id == applicant_team_id or target_dept_id == applicant_dept_id:
            # 本团队 或 本部门其他团队:资源团队组长提单自动生效
            return []
        # 跨部门团队:资源团队组长提单 + 资源部门经理批准(缺失降级)
        approver_role, s_type, s_id = _resolve_dept_leader(target_dept_id, exclude_user_id=applicant_id)
        return [_build_chain_node(approver_role, s_type, s_id)]

    # ── team_leader 申请(授权组长:部门经理发起流程,人员管理审批,超管兜底) ──
    # 定稿:任命组长由部门经理发起流程,审批人是人员管理(user_admin),超管兜底。
    # 审批池为全局角色,不绑定具体组织;发起人为 user_admin 时由另一 user_admin 审批(回避原则)。
    if role_key == 'team_leader':
        # team_leader 是 TEAM_SCOPE 角色,scope 必须绑定目标团队,否则授权语义错误
        if scope_type != ScopeType.TEAM or not scope_id:
            return [_build_chain_node(ApproverRole.SUPER_ADMIN)]
        # 目标团队不存在/已删除 → 兜底超管单审(避免对失效团队产生可审批工单)
        if not Team.objects.filter(id=scope_id, is_deleted=False).exists():
            return [_build_chain_node(ApproverRole.SUPER_ADMIN)]
        has_user_admin = UserRoleRel.objects.filter(
            role__role_key='user_admin', status=GrantStatus.ACTIVE,
        ).exclude(user_id=applicant_id or 0).exists()
        if has_user_admin:
            return [_build_chain_node(ApproverRole.USER_ADMIN)]
        # 无可用用户管理员 → 超管兜底单审
        return [_build_chain_node(ApproverRole.SUPER_ADMIN)]

    # 兜底:未知角色 → 超管单审
    return [_build_chain_node(ApproverRole.SUPER_ADMIN)]


def _build_revoke_chain_for_team_role(applicant, role_key: str,
                                       scope_type: str, scope_id) -> list:
    """构造 team_leader / contributor / viewer 的撤销(REVOKE)审批链(方案 B 加严)

    规则矩阵(撤销比申请更严格):
    - viewer 跨团队       → 无需审批,直接生效(返回空链)
    - contributor 任意    → 无需审批,直接生效(返回空链)
    - team_leader 本部门  → 本部门经理 → 用户管理员 双审
    - team_leader 跨部门  → 本部门经理 → 目标部门经理 双审

    业务背景:撤销团队组长会让人失去管理权,需要本部门经理知情 + 用户管理员复核,
    避免恶意撤销导致团队失控。
    """
    applicant_id = applicant.id if applicant else None
    applicant_dept_id = applicant.department_id if applicant else None

    # viewer 跨团队 / contributor:无需审批,直接生效
    if role_key in ('viewer', 'contributor'):
        return []

    # team_leader:本部门 → 本部门经理 + 用户管理员;跨部门 → 本部门经理 + 目标部门经理
    # team_leader 是 TEAM_SCOPE 角色,撤销 scope 绑定目标团队(scope_id=team_id),
    # 本部门/跨部门按"目标团队所属部门"与"申请人(操作者)部门"对比判定。
    if role_key == 'team_leader':
        if scope_type != ScopeType.TEAM or not scope_id:
            return [_build_chain_node(ApproverRole.SUPER_ADMIN)]
        target_team = Team.objects.filter(id=scope_id, is_deleted=False).only('department_id').first()
        if not target_team or not target_team.department_id:
            # 目标团队无部门归属,无法构造部门审批链,兜底超管单审
            return [_build_chain_node(ApproverRole.SUPER_ADMIN)]
        target_dept_id = target_team.department_id
        is_cross = (target_dept_id != applicant_dept_id)
        # 节点1:本部门经理(降级到用户管理员)
        role1, t1, i1 = _resolve_dept_leader(applicant_dept_id, exclude_user_id=applicant_id) \
            if applicant_dept_id else (ApproverRole.USER_ADMIN, ScopeType.NONE, None)
        if is_cross:
            # 跨部门:节点2 = 目标部门经理(降级到用户管理员)
            role2, t2, i2 = _resolve_dept_leader(target_dept_id, exclude_user_id=applicant_id)
        else:
            # 本部门:节点2 = 用户管理员(本部门经理已审,加用户管理员复核)
            role2, t2, i2 = (ApproverRole.USER_ADMIN, ScopeType.NONE, None)
        return [
            _build_chain_node(role1, t1, i1),
            _build_chain_node(role2, t2, i2),
        ]

    # 兜底:未知角色 → 超管单审
    return [_build_chain_node(ApproverRole.SUPER_ADMIN)]

