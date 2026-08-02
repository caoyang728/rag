"""
apps.users.ticket_service - 权限配置审批工单服务

审批规则（对齐 RAG_RBAC_权限架构设计.md 最终计划）：
- 同部门授权（GRANT team_leader/employee，目标用户与申请人同团队）：团队组长单审即可
- 跨部门/跨团队/全局角色：双轨审核（一审 + 二审）
- super_admin 新增/撤销：强制另一个 super_admin 双人复核
- 降级/撤销（REVOKE）：团队组长可直接执行，无需审批（但记审计）
- 任一节点 REJECTED → 工单终态 REJECTED，不执行授权表写入
- 审批工单永不删除，只改状态

工单流转状态机：
  PENDING --approve(末节点)--> APPROVED --execute(异步/同步)--> EXECUTED
  PENDING --reject--> REJECTED（终态）
  PENDING --cancel(发起人)--> CANCELLED（终态）

审批链 approval_chain 结构（JSONField，顺序执行，共享审批池模式）：
  [
    {"approver_role": "TEAM_LEADER", "status": "PENDING",
     "approver_id": null, "approved_at": null, "comment": ""},
    ...
  ]
  - approver_role：审批人角色定位（TEAM_LEADER / DEPT_LEADER / SUPER_ADMIN）
    创建时锁定角色类型，不锁定具体审批人（共享审批池 + 先到先得）
  - approver_id：审批时回填（谁先处理就锁定谁，防止并发审批）
  - status：PENDING / APPROVED / REJECTED
  - 顺序执行：current_step 指向待审批节点，前一节点 APPROVED 才到下一节点
"""
import uuid
from typing import Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from loguru import logger

from apps.users.models import (
    PermissionApprovalTicket, PermissionAuditLog,
    UserRoleRel, UserDeptScopeRel, UserTeamScopeRel,
    Role, User, Department, Team,
    TicketStatus, TicketChangeType, ScopeType, RoleType, GrantStatus,
    AuditTargetType, RoleConflictRule,
)


# ============================================================================
# 审批人角色定位（用于审批链快照与前端展示）
# ============================================================================
class ApproverRole:
    """审批人在审批链中的角色定位 —— 决定该节点由谁审批

    新增 USER_ADMIN 后审批节点匹配规则:
    - TEAM_LEADER / DEPT_LEADER:基于组织架构(leader_id)匹配,带 scope 区分本团队/目标团队
    - USER_ADMIN:持有 user_admin 角色的用户(用于部门经理/文档管理员/合规管理员审批链)
    - SUPER_ADMIN:持有 super_admin 角色的用户(用于全局高权角色审批链 / 兜底)
    """
    TEAM_LEADER = 'TEAM_LEADER'    # 团队组长(单审 / 跨团队一审)
    DEPT_LEADER = 'DEPT_LEADER'    # 部门负责人(团队组长审批 / 跨部门二审)
    USER_ADMIN = 'USER_ADMIN'      # 用户管理员(部门经理/文档管理员/合规管理员审批链第一节点)
    SUPER_ADMIN = 'SUPER_ADMIN'    # 超级管理员(全局高权角色审批链第二节点 / super_admin 双人复核)


class ApproveStepStatus:
    """审批节点状态 —— 与 GrantStatus 解耦，仅用于审批链内部流转"""
    PENDING = 'PENDING'
    APPROVED = 'APPROVED'
    REJECTED = 'REJECTED'


# ============================================================================
# 角色等级与互斥组(用于 ROLE_CHANGE 流向判定 + SoD 校验)
# ============================================================================
# 4 个全局高权角色两两互斥(4 选 1),任一用户最多持有 1 个
GLOBAL_HIGH_PRIVILEGE_KEYS = ('user_admin', 'kb_admin', 'compliance_admin', 'super_admin')

# 团队级角色等级(同 scope 内高等级覆盖低等级,ROLE_CHANGE 流向判定用)
# viewer < contributor < team_leader
TEAM_ROLE_RANK = {'viewer': 1, 'contributor': 2, 'team_leader': 3}
# 团队级角色 key 集合(同团队内互斥,用于 create_ticket 自动检测 ROLE_CHANGE)
TEAM_ROLE_KEYS = tuple(TEAM_ROLE_RANK.keys())


# ============================================================================
# 审计动作常量（对齐 PermissionAuditLog.action 清单）
# ============================================================================
class AuditAction:
    TICKET_CREATE = 'TICKET_CREATE'
    TICKET_APPROVE = 'TICKET_APPROVE'
    TICKET_REJECT = 'TICKET_REJECT'
    TICKET_CANCEL = 'TICKET_CANCEL'
    TICKET_EXECUTE = 'TICKET_EXECUTE'
    ROLE_GRANT = 'ROLE_GRANT'
    ROLE_REVOKE = 'ROLE_REVOKE'
    SCOPE_GRANT = 'SCOPE_GRANT'
    SCOPE_REVOKE = 'SCOPE_REVOKE'
    ROLE_CHANGE = 'ROLE_CHANGE'    # 角色变更(原子撤销旧角色 + 授予新角色)


# ============================================================================
# 审批人角色匹配 —— 共享审批池的核心：判定用户是否具备某审批节点所需的角色
# ============================================================================

def _can_approve_for_role(user, approver_role: str, ticket=None) -> bool:
    """判定用户是否能审批指定 approver_role 的节点 —— 共享审批池的核心校验

    各 approver_role 对应的判定逻辑:
    - SUPER_ADMIN:用户持有 super_admin 角色(用于超管工单双审 / 全局角色二审)
    - USER_ADMIN:用户持有 user_admin 角色(用于部门经理/文档管理员/合规管理员工单一审)
        注:用户管理员 × 超管互斥,故 USER_ADMIN 与 SUPER_ADMIN 节点的候选池天然不重叠
    - TEAM_LEADER:用户是审批节点指定 scope 的团队组长(team.leader_id == user.id)
        节点带 approver_scope_id,区分"本团队组长"和"目标团队组长"
    - DEPT_LEADER:用户是审批节点指定 scope 的部门负责人(department.leader_id == user.id)
        节点带 approver_scope_id,区分"本部门经理"和"目标部门经理"

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
    if ticket and ticket.approval_chain:
        approved_user_ids = {
            n.get('approver_id') for n in ticket.approval_chain[:ticket.current_step]
            if n.get('approver_id')
        }
        if user.id in approved_user_ids:
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
        # 持有 user_admin 角色即可(部门经理/文档管理员/合规管理员工单一审)
        # 注:user_admin × super_admin 互斥,此处不会误匹配超管
        return UserRoleRel.objects.filter(
            user=user, role__role_key='user_admin',
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
        return Department.objects.filter(
            id=node_dept_id, is_deleted=False, leader_id=user.id,
        ).exists()

    return False


def _find_approver_ids_for_role(approver_role: str, ticket=None) -> list:
    """查找能审批指定 approver_role 节点的所有用户 ID —— 用于待办列表查询

    共享审批池模式:返回所有符合 approver_role 的用户 ID,前端按"先到先得"展示。
    排除申请人/目标用户(回避原则) + 排除已审过前序节点的人(双人独立性)。
    """
    # 计算需排除的用户 ID 集合(申请人/目标用户/前序节点审批人)
    exclude_ids = set()
    if ticket:
        if ticket.applicant_id:
            exclude_ids.add(ticket.applicant_id)
        if ticket.target_user_id:
            exclude_ids.add(ticket.target_user_id)
        # 双人独立性:前序节点审批人不能再审后续节点
        if ticket.approval_chain:
            for n in ticket.approval_chain[:ticket.current_step]:
                if n.get('approver_id'):
                    exclude_ids.add(n['approver_id'])

    if approver_role == ApproverRole.SUPER_ADMIN:
        ids = _get_super_admin_ids(exclude_user_id=ticket.applicant_id if ticket else None)
        return [i for i in ids if i not in exclude_ids]

    if approver_role == ApproverRole.USER_ADMIN:
        # 查所有持有 user_admin 角色的活跃用户
        ids = list(UserRoleRel.objects.filter(
            role__role_key='user_admin', status=GrantStatus.ACTIVE,
        ).exclude(user_id__in=exclude_ids).values_list('user_id', flat=True).distinct())
        return ids

    # 取当前节点(读取 approver_scope_id)
    current_node = None
    if ticket and ticket.approval_chain and ticket.current_step < len(ticket.approval_chain):
        current_node = ticket.approval_chain[ticket.current_step]

    if approver_role == ApproverRole.TEAM_LEADER:
        if not current_node:
            return []
        node_team_id = current_node.get('approver_scope_id')
        if not node_team_id:
            return []
        team = Team.objects.filter(id=node_team_id, is_deleted=False).only('leader_id').first()
        if not team or not team.leader_id or team.leader_id in exclude_ids:
            return []
        return [team.leader_id]

    if approver_role == ApproverRole.DEPT_LEADER:
        if not current_node:
            return []
        node_dept_id = current_node.get('approver_scope_id')
        if not node_dept_id:
            return []
        leader_id = Department.objects.filter(
            id=node_dept_id, is_deleted=False,
        ).values_list('leader_id', flat=True).first()
        if not leader_id or leader_id in exclude_ids:
            return []
        return [leader_id]

    return []


# ============================================================================
# 审批链构造：根据变更类型 + 范围决定走单审 / 双轨 / 直接执行
# ============================================================================

def _get_team_leader_id(team_id) -> Optional[int]:
    """获取团队组长 ID —— 单审/一审审批人

    组长可能为空（团队刚建立未指派），此时退化为该团队所属部门负责人审批。
    """
    if not team_id:
        return None
    team = Team.objects.filter(id=team_id).only('leader_id', 'department_id').first()
    if team and team.leader_id:
        return team.leader_id
    return None


def _get_dept_leader_id(dept_id) -> Optional[int]:
    """获取部门负责人 ID —— 二审审批人"""
    if not dept_id:
        return None
    return Department.objects.filter(id=dept_id).values_list('leader_id', flat=True).first()


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
    """检测用户在指定团队内已持有的团队角色 —— 服务层互斥检测

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


def _build_super_admin_chain_2step(applicant) -> list:
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
    - viewer 本团队       → 自动授予(节点同步,不进工单,本函数不处理)
    - viewer 跨团队       → 目标团队组长单审(缺失降级)
    - contributor 本团队  → 本团队组长单审(缺失降级)
    - contributor 跨团队  → 本团队组长 → 目标团队组长 双审
    - team_leader 本部门  → 本部门经理单审(缺失降级)
    - team_leader 跨部门  → 本部门经理 → 目标部门经理 双审
    - dept_manager        → 用户管理员 → 超管 双审
    - kb_admin            → 用户管理员 → 超管 双审
    - compliance_admin    → 用户管理员 → 超管 双审
    - user_admin          → 双超管(排除申请人)
    - super_admin         → 双超管(排除申请人,强制双人独立)

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
        return _build_super_admin_chain_2step(applicant)

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

    规则矩阵:
    - viewer 跨团队       → 目标团队组长单审(缺失降级)
    - contributor 本团队  → 本团队组长单审(缺失降级)
    - contributor 跨团队  → 本团队组长 → 目标团队组长 双审
    - team_leader 本部门  → 本部门经理单审(缺失降级)
    - team_leader 跨部门  → 本部门经理 → 目标部门经理 双审

    跨部门/跨团队判定:对比 applicant.team_id/department_id 与 scope_id
    申请人无 team_id → 默认跨部门,本团队节点降级到本部门经理

    is_role_change=True 时,本团队节点会触发回避(申请人自己),自动走降级链。
    """
    applicant_id = applicant.id if applicant else None
    applicant_team_id = applicant.team_id if applicant else None
    applicant_dept_id = applicant.department_id if applicant else None

    # ── viewer 申请(仅跨团队场景进工单) ──
    if role_key == 'viewer':
        # viewer 本团队自动授予,不进工单;进到这里默认是跨团队
        if scope_type != ScopeType.TEAM or not scope_id:
            # viewer 跨团队必须有 scope_id,异常情况降级到超管兜底
            return [_build_chain_node(ApproverRole.SUPER_ADMIN)]
        # 目标团队组长单审(缺失降级)
        approver_role, s_type, s_id = _resolve_team_leader(scope_id, exclude_user_id=applicant_id)
        return [_build_chain_node(approver_role, s_type, s_id)]

    # ── contributor 申请(本团队单审 / 跨团队双审) ──
    if role_key == 'contributor':
        if scope_type != ScopeType.TEAM or not scope_id:
            return [_build_chain_node(ApproverRole.SUPER_ADMIN)]
        is_cross = (scope_id != applicant_team_id)
        if not is_cross:
            # 本团队:本团队组长单审
            approver_role, s_type, s_id = _resolve_team_leader(scope_id, exclude_user_id=applicant_id)
            return [_build_chain_node(approver_role, s_type, s_id)]
        # 跨团队:本团队组长 → 目标团队组长 双审
        # 本团队组长节点(申请人无 team_id 时降级到本部门经理)
        if applicant_team_id:
            role1, t1, i1 = _resolve_team_leader(applicant_team_id, exclude_user_id=applicant_id)
        else:
            # 申请人无 team_id:本团队节点降级到本部门经理
            if applicant_dept_id:
                role1, t1, i1 = _resolve_dept_leader(applicant_dept_id, exclude_user_id=applicant_id)
            else:
                role1, t1, i1 = (ApproverRole.USER_ADMIN, ScopeType.NONE, None)
        # 目标团队组长节点
        role2, t2, i2 = _resolve_team_leader(scope_id, exclude_user_id=applicant_id)
        return [
            _build_chain_node(role1, t1, i1),
            _build_chain_node(role2, t2, i2),
        ]

    # ── team_leader 申请(本部门单审 / 跨部门双审) ──
    if role_key == 'team_leader':
        if scope_type != ScopeType.DEPT or not scope_id:
            return [_build_chain_node(ApproverRole.SUPER_ADMIN)]
        is_cross = (scope_id != applicant_dept_id)
        if not is_cross:
            # 本部门:本部门经理单审
            approver_role, s_type, s_id = _resolve_dept_leader(scope_id, exclude_user_id=applicant_id)
            return [_build_chain_node(approver_role, s_type, s_id)]
        # 跨部门:本部门经理 → 目标部门经理 双审
        role1, t1, i1 = _resolve_dept_leader(applicant_dept_id, exclude_user_id=applicant_id) \
            if applicant_dept_id else (ApproverRole.USER_ADMIN, ScopeType.NONE, None)
        role2, t2, i2 = _resolve_dept_leader(scope_id, exclude_user_id=applicant_id)
        return [
            _build_chain_node(role1, t1, i1),
            _build_chain_node(role2, t2, i2),
        ]

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
    if role_key == 'team_leader':
        if scope_type != ScopeType.DEPT or not scope_id:
            return [_build_chain_node(ApproverRole.SUPER_ADMIN)]
        is_cross = (scope_id != applicant_dept_id)
        # 节点1:本部门经理(降级到用户管理员)
        role1, t1, i1 = _resolve_dept_leader(applicant_dept_id, exclude_user_id=applicant_id) \
            if applicant_dept_id else (ApproverRole.USER_ADMIN, ScopeType.NONE, None)
        if is_cross:
            # 跨部门:节点2 = 目标部门经理(降级到用户管理员)
            role2, t2, i2 = _resolve_dept_leader(scope_id, exclude_user_id=applicant_id)
        else:
            # 本部门:节点2 = 用户管理员(本部门经理已审,加用户管理员复核)
            role2, t2, i2 = (ApproverRole.USER_ADMIN, ScopeType.NONE, None)
        return [
            _build_chain_node(role1, t1, i1),
            _build_chain_node(role2, t2, i2),
        ]

    # 兜底:未知角色 → 超管单审
    return [_build_chain_node(ApproverRole.SUPER_ADMIN)]


def _is_same_team(applicant, target_user, team_id) -> bool:
    """申请人与目标用户是否同属指定团队(用于判定单审/双轨)

    同团队内授权走单审;跨团队走双轨。
    保留供旧代码兼容,新代码已直接对比 applicant.team_id 与 scope_id。
    """
    if not target_user:
        return False
    return applicant.team_id == team_id and target_user.team_id == team_id


def _get_team_dept_id(team_id):
    """获取团队所属部门 ID"""
    if not team_id:
        return None
    return Team.objects.filter(id=team_id).values_list('department_id', flat=True).first()


# ============================================================================
# 工单创建与流转
# ============================================================================

def _gen_ticket_no() -> str:
    """生成全局唯一工单号：T + 日期 + 短 UUID，便于人工沟通与检索"""
    return 'T' + timezone.localtime().strftime('%Y%m%d') + uuid.uuid4().hex[:8].upper()


@transaction.atomic
def create_ticket(applicant, target_user, change_type: str,
                  role: Role, scope_type: str = ScopeType.NONE, scope_id=None,
                  effective_from=None, expires_at=None, reason: str = '',
                  ip_address: str = '', user_agent: str = '',
                  previous_role: Role = None) -> PermissionApprovalTicket:
    """创建审批工单 —— 授权变更统一入口

    流程:
    1. 入口校验:SoD 互斥检查(4 高权 4 选 1) + 超管硬约束(可用超管 ≥2)
    2. 构造审批链(build_approval_chain)
    3. 空链 → REVOKE 普通角色:直接执行撤销并返回已执行工单(记审计)
    4. 非空链 → 创建 PENDING 工单,等待逐级审批
    5. 写 TICKET_CREATE 审计

    返回:工单对象(已执行或待审批)

    :param previous_role:仅 ROLE_CHANGE 使用,记录变更前旧角色(执行时撤销目标)
    """
    # ── 入口校验 1:SoD 互斥(仅 GRANT / ROLE_CHANGE 需要校验,REVOKE 是减少角色不冲突) ──
    if change_type in (TicketChangeType.GRANT, TicketChangeType.ROLE_CHANGE) and role:
        _check_sod_conflict(target_user, role)

    # ── 入口校验 2:双超管硬约束(super_admin / user_admin 工单需双超管审批,可用超管 <2 直接拒绝) ──
    # user_admin 走双超管链(与 super_admin 同),配额不足时工单会卡死,故入口拒绝
    if role and role.role_key in ('super_admin', 'user_admin'):
        _check_super_admin_quota(applicant)

    # ── 入口校验 3:团队级互斥自动转 ROLE_CHANGE ──
    # 业务规则:同团队内团队角色(viewer/contributor/team_leader)互斥,
    # 高等级覆盖低等级。申请同团队新角色时,若已有旧角色,自动转为 ROLE_CHANGE
    # (原子撤销旧角色 + 授予新角色),避免同团队出现多条 ACTIVE 记录违反 DB 唯一约束。
    # 此校验下沉到 create_ticket,确保所有工单创建路径(自助申请/管理员工单)统一拦截。
    if (change_type == TicketChangeType.GRANT
            and role and role.role_key in TEAM_ROLE_KEYS
            and scope_type == ScopeType.TEAM and scope_id):
        existing_role = _detect_team_role_in_service(target_user, scope_id)
        if existing_role and existing_role.id != role.id:
            previous_role = existing_role
            change_type = TicketChangeType.ROLE_CHANGE
            logger.info(
                f'[Ticket] 团队级互斥自动转 ROLE_CHANGE: '
                f'user={target_user.username} team_id={scope_id} '
                f'{existing_role.role_key} -> {role.role_key}'
            )

    chain = build_approval_chain(applicant, target_user, change_type,
                                  role, scope_type, scope_id,
                                  previous_role=previous_role)

    # 空审批链:降级/撤销低权角色 → 直接执行(viewer 跨团队撤销/contributor 撤销)
    if not chain:
        ticket = PermissionApprovalTicket.objects.create(
            ticket_no=_gen_ticket_no(),
            applicant=applicant,
            target_user=target_user,
            change_type=change_type,
            role=role,
            previous_role=previous_role,
            scope_type=scope_type,
            scope_id=scope_id,
            effective_from=effective_from,
            expires_at=expires_at,
            reason=reason,
            approval_chain=[],
            current_step=0,
            status=TicketStatus.EXECUTED,
            approved_at=timezone.now(),
            executed_at=timezone.now(),
        )
        _execute_grant_or_revoke(ticket, actor=applicant)
        _write_audit(ticket, applicant, AuditAction.TICKET_CREATE,
                     ip_address, user_agent, result='SUCCESS')
        _write_audit(ticket, applicant, AuditAction.TICKET_EXECUTE,
                     ip_address, user_agent, result='SUCCESS')
        logger.info(f'[Ticket] 直接执行(无审批链): {change_type} '
                    f'{role.role_key if role else "-"} -> {target_user.id}')
        return ticket

    # 非空审批链:创建待审批工单
    ticket = PermissionApprovalTicket.objects.create(
        ticket_no=_gen_ticket_no(),
        applicant=applicant,
        target_user=target_user,
        change_type=change_type,
        role=role,
        previous_role=previous_role,
        scope_type=scope_type,
        scope_id=scope_id,
        effective_from=effective_from,
        expires_at=expires_at,
        reason=reason,
        approval_chain=chain,
        current_step=0,
        status=TicketStatus.PENDING,
    )
    _write_audit(ticket, applicant, AuditAction.TICKET_CREATE,
                 ip_address, user_agent, result='SUCCESS')
    logger.info(f'[Ticket] 创建待审批工单: {ticket.ticket_no} '
                f'approvers={[n.get("approver_role") for n in chain]}')
    return ticket


@transaction.atomic
def approve_ticket(ticket: PermissionApprovalTicket, approver: User,
                   comment: str = '', ip_address: str = '', user_agent: str = '') -> PermissionApprovalTicket:
    """审批通过当前节点 —— 共享审批池模式：任一符合 approver_role 的用户均可审批，先到先得

    校验（共享审批池 + 先到先得）：
    - 工单必须 PENDING
    - approver 必须具备当前节点 approver_role 所要求的角色/身份
    - 审批时回填 approver_id（锁定审批人，防止并发审批）
    - 不允许跨节点审批
    - select_for_update 防并发：两个管理员同时审批时只有一个能成功

    末节点通过 → status=APPROVED → 同步执行授权写入 → status=EXECUTED
    """
    # select_for_update 防止并发审批：同一工单同时只能被一个事务修改
    ticket = PermissionApprovalTicket.objects.select_for_update().get(pk=ticket.pk)

    if ticket.status != TicketStatus.PENDING:
        raise ValueError(f'工单非待审批状态: {ticket.status}')

    chain = ticket.approval_chain or []
    if ticket.current_step >= len(chain):
        raise ValueError('审批链已完结，无待审批节点')

    node = chain[ticket.current_step]
    approver_role = node['approver_role']

    # 共享审批池校验：判定 approver 是否具备该节点 approver_role 所需的角色/身份
    # 如果节点已有 approver_id（被其他管理员先处理），则拒绝
    if node.get('approver_id') and node['approver_id'] != approver.id:
        raise PermissionError('该工单已被其他管理员处理，不再属于您的待办')

    # 角色匹配校验：approver 必须具备对应 approver_role 的权限
    if not _can_approve_for_role(approver, approver_role, ticket):
        raise PermissionError(f'您没有审批 {approver_role} 类型工单的权限')

    # 回填 approver_id（锁定审批人，防止其他人再审批此节点）
    now = timezone.now()
    node['approver_id'] = approver.id
    node['status'] = ApproveStepStatus.APPROVED
    node['approved_at'] = now.isoformat()
    node['comment'] = comment
    ticket.approval_chain = chain  # 触发 JSONField 保存

    _write_audit(ticket, approver, AuditAction.TICKET_APPROVE,
                 ip_address, user_agent, result='SUCCESS',
                 extra={'step': ticket.current_step, 'approver_role': approver_role})

    # 末节点通过 → 工单通过 → 执行授权写入
    if ticket.current_step >= len(chain) - 1:
        ticket.status = TicketStatus.APPROVED
        ticket.approved_at = now
        ticket.save()
        _execute_grant_or_revoke(ticket, actor=approver)
        ticket.status = TicketStatus.EXECUTED
        ticket.executed_at = timezone.now()
        ticket.save()
        _write_audit(ticket, approver, AuditAction.TICKET_EXECUTE,
                     ip_address, user_agent, result='SUCCESS')
        logger.info(f'[Ticket] 工单审批通过并执行: {ticket.ticket_no} '
                    f'approver={approver.id} role={approver_role}')
    else:
        # 推进到下一节点
        ticket.current_step += 1
        ticket.save()
        logger.info(f'[Ticket] 工单节点通过，推进下一节点: {ticket.ticket_no} step={ticket.current_step}')
    return ticket


@transaction.atomic
def reject_ticket(ticket: PermissionApprovalTicket, rejector: User,
                  comment: str = '', ip_address: str = '', user_agent: str = '') -> PermissionApprovalTicket:
    """驳回工单 —— 共享审批池模式：任一符合 approver_role 的用户均可驳回

    驳回人可以是当前节点审批人（角色匹配），或 super_admin（兜底越级驳回）。
    """
    ticket = PermissionApprovalTicket.objects.select_for_update().get(pk=ticket.pk)

    if ticket.status != TicketStatus.PENDING:
        raise ValueError(f'工单非待审批状态: {ticket.status}')

    chain = ticket.approval_chain or []
    # 当前节点审批人（角色匹配）或 super_admin 可驳回
    can_reject = False
    if ticket.current_step < len(chain):
        node = chain[ticket.current_step]
        if node.get('approver_id') and node['approver_id'] == rejector.id:
            can_reject = True
        elif _can_approve_for_role(rejector, node['approver_role'], ticket):
            can_reject = True
    if not can_reject and not rejector.is_super_admin:
        raise PermissionError('无权驳回该工单')

    if ticket.current_step < len(chain):
        chain[ticket.current_step]['status'] = ApproveStepStatus.REJECTED
        chain[ticket.current_step]['approver_id'] = rejector.id
        chain[ticket.current_step]['comment'] = comment
        ticket.approval_chain = chain

    ticket.status = TicketStatus.REJECTED
    ticket.save()
    _write_audit(ticket, rejector, AuditAction.TICKET_REJECT,
                 ip_address, user_agent, result='SUCCESS',
                 extra={'comment': comment})
    logger.info(f'[Ticket] 工单被驳回: {ticket.ticket_no} by={rejector.id}')
    return ticket


@transaction.atomic
def cancel_ticket(ticket: PermissionApprovalTicket, actor: User,
                  ip_address: str = '', user_agent: str = '') -> PermissionApprovalTicket:
    """发起人撤回工单 —— 仅 PENDING 状态可撤回，已执行不可撤

    防止授权已生效后撤回工单造成状态不一致。
    """
    if ticket.status != TicketStatus.PENDING:
        raise ValueError('仅待审批工单可撤回')
    if ticket.applicant_id != actor.id and not actor.is_super_admin:
        raise PermissionError('仅发起人可撤回工单')

    ticket.status = TicketStatus.CANCELLED
    ticket.save()
    _write_audit(ticket, actor, AuditAction.TICKET_CANCEL,
                 ip_address, user_agent, result='SUCCESS')
    logger.info(f'[Ticket] 工单撤回: {ticket.ticket_no} by={actor.id}')
    return ticket


# ============================================================================
# 工单执行：审批通过后写入授权表（GRANT）或撤销授权（REVOKE）
# ============================================================================

def _execute_grant_or_revoke(ticket: PermissionApprovalTicket, actor: User):
    """执行授权写入 —— 工单 APPROVED 后调用

    GRANT:根据 scope_type 写入对应授权表,status=ACTIVE
    REVOKE:将对应授权记录置 status=REVOKED + revoked_at
    SCOPE_CHANGE:先撤销旧 scope 授权,再写入新 scope
    EXPIRE_EXTEND:更新 expires_at
    ROLE_CHANGE:原子操作 —— 撤销 previous_role + 授予 role(同 scope)

    幂等:通过 ticket.status=EXECUTED 防重复执行(调用前已置 APPROVED)。
    """
    if ticket.change_type in (TicketChangeType.GRANT, TicketChangeType.SCOPE_CHANGE):
        _apply_grant(ticket, actor)
    elif ticket.change_type == TicketChangeType.REVOKE:
        _apply_revoke(ticket, actor)
    elif ticket.change_type == TicketChangeType.EXPIRE_EXTEND:
        _apply_extend(ticket, actor)
    elif ticket.change_type == TicketChangeType.ROLE_CHANGE:
        _apply_role_change(ticket, actor)


def _apply_role_change(ticket: PermissionApprovalTicket, actor: User):
    """角色变更执行 —— 原子操作:撤销旧角色(previous_role) + 授予新角色(role)

    业务背景:用户在同一 scope 内变更角色(如 viewer → contributor),
    不能"先撤销后申请"两步走(中间状态会失去权限),必须原子完成。

    流程(全部在同一事务内,任一步失败回滚):
    1. 撤销 previous_role 在 ticket.scope_type/scope_id 下的授权记录
    2. 授予 role(新角色)在 ticket.scope_type/scope_id 下的授权记录
    3. 写 ROLE_CHANGE 审计(包含 previous_role → role 快照)

    边界:
    - previous_role 为空时仅授予新角色(降级到 GRANT 语义)
    - 撤销时只命中 ACTIVE 状态的旧授权,PENDING/REVOKED 不动
    """
    now = timezone.now()

    # 1) 撤销旧角色(若存在)
    if ticket.previous_role_id:
        prev_role = ticket.previous_role
        # 全局角色表
        if UserRoleRel.objects.filter(
            user=ticket.target_user, role=prev_role, status=GrantStatus.ACTIVE,
        ).update(status=GrantStatus.REVOKED, revoked_at=now, revoked_by=actor, ticket=ticket):
            pass
        # 部门属地
        if ticket.scope_type in (ScopeType.DEPT, ScopeType.NONE):
            qs = UserDeptScopeRel.objects.filter(
                user=ticket.target_user, role=prev_role, status=GrantStatus.ACTIVE,
            )
            if ticket.scope_type == ScopeType.DEPT and ticket.scope_id:
                qs = qs.filter(dept_id=ticket.scope_id)
            qs.update(status=GrantStatus.REVOKED, revoked_at=now, revoked_by=actor, ticket=ticket)
        # 团队属地
        if ticket.scope_type in (ScopeType.TEAM, ScopeType.NONE):
            qs = UserTeamScopeRel.objects.filter(
                user=ticket.target_user, role=prev_role, status=GrantStatus.ACTIVE,
            )
            if ticket.scope_type == ScopeType.TEAM and ticket.scope_id:
                qs = qs.filter(team_id=ticket.scope_id)
            qs.update(status=GrantStatus.REVOKED, revoked_at=now, revoked_by=actor, ticket=ticket)

    # 2) 授予新角色(复用 _apply_grant 逻辑)
    _apply_grant(ticket, actor)

    # 3) 写 ROLE_CHANGE 审计(独立于 _apply_grant 的 ROLE_GRANT 审计,便于回溯)
    _write_audit(ticket, actor, AuditAction.ROLE_CHANGE, '', '', result='SUCCESS',
                 extra={
                     'previous_role': ticket.previous_role.role_key if ticket.previous_role else None,
                     'new_role': ticket.role.role_key if ticket.role else None,
                 })
    logger.info(f'[Ticket] 角色变更执行: {ticket.ticket_no} '
                f'{ticket.previous_role.role_key if ticket.previous_role else "-"} → '
                f'{ticket.role.role_key if ticket.role else "-"} '
                f'for user {ticket.target_user_id}')


def _apply_grant(ticket: PermissionApprovalTicket, actor: User):
    """写入授权表（GRANT/SCOPE_CHANGE）—— 根据 scope_type 分发到三张授权表

    - scope_type=NONE + 全局角色 → UserRoleRel
    - scope_type=DEPT → UserDeptScopeRel
    - scope_type=TEAM → UserTeamScopeRel

    查找条件带 status=ACTIVE:
    - 团队/部门级改为 (user, team/dept) ACTIVE 唯一约束后,可能存在多条历史 REVOKED 记录,
      update_or_create 不带 status 会触发 MultipleObjectsReturned,故只查 ACTIVE 记录。
    - 找到 ACTIVE → update(复用已有记录);找不到 → create(新记录)。
    - 若同 scope 已有不同 role 的 ACTIVE 记录(应用层互斥校验漏了),
      create 会触发 DB 唯一约束报错(EAFP 兜底)。
    """
    common = dict(
        user=ticket.target_user,
        role=ticket.role,
        granted_by=actor,
        effective_from=ticket.effective_from,
        expires_at=ticket.expires_at,
        status=GrantStatus.ACTIVE,
        ticket=ticket,
    )
    if ticket.scope_type == ScopeType.DEPT and ticket.scope_id:
        UserDeptScopeRel.objects.update_or_create(
            user=ticket.target_user, role=ticket.role, dept_id=ticket.scope_id,
            status=GrantStatus.ACTIVE,
            defaults=common,
        )
        action = AuditAction.SCOPE_GRANT
    elif ticket.scope_type == ScopeType.TEAM and ticket.scope_id:
        UserTeamScopeRel.objects.update_or_create(
            user=ticket.target_user, role=ticket.role, team_id=ticket.scope_id,
            status=GrantStatus.ACTIVE,
            defaults=common,
        )
        action = AuditAction.SCOPE_GRANT
    else:
        # 全局角色（scope_type=NONE）— UserRoleRel 为 (user, role) 绝对唯一,无需带 status
        UserRoleRel.objects.update_or_create(
            user=ticket.target_user, role=ticket.role,
            defaults=common,
        )
        action = AuditAction.ROLE_GRANT
    _write_audit(ticket, actor, action, '', '', result='SUCCESS')


def _apply_revoke(ticket: PermissionApprovalTicket, actor: User):
    """撤销授权（REVOKE）—— 将对应授权记录置 REVOKED

    逐表尝试撤销（一个用户同一角色可能跨表存在），全部命中即撤销。
    """
    now = timezone.now()
    revoked = False
    # 全局角色
    if UserRoleRel.objects.filter(
        user=ticket.target_user, role=ticket.role, status=GrantStatus.ACTIVE,
    ).update(status=GrantStatus.REVOKED, revoked_at=now, revoked_by=actor, ticket=ticket):
        revoked = True
    # 部门属地
    if ticket.scope_type in (ScopeType.DEPT, ScopeType.NONE):
        qs = UserDeptScopeRel.objects.filter(
            user=ticket.target_user, role=ticket.role, status=GrantStatus.ACTIVE,
        )
        if ticket.scope_type == ScopeType.DEPT and ticket.scope_id:
            qs = qs.filter(dept_id=ticket.scope_id)
        if qs.update(status=GrantStatus.REVOKED, revoked_at=now, revoked_by=actor, ticket=ticket):
            revoked = True
    # 团队属地
    if ticket.scope_type in (ScopeType.TEAM, ScopeType.NONE):
        qs = UserTeamScopeRel.objects.filter(
            user=ticket.target_user, role=ticket.role, status=GrantStatus.ACTIVE,
        )
        if ticket.scope_type == ScopeType.TEAM and ticket.scope_id:
            qs = qs.filter(team_id=ticket.scope_id)
        if qs.update(status=GrantStatus.REVOKED, revoked_at=now, revoked_by=actor, ticket=ticket):
            revoked = True

    action = AuditAction.ROLE_REVOKE if ticket.scope_type == ScopeType.NONE else AuditAction.SCOPE_REVOKE
    _write_audit(ticket, actor, action, '', '',
                 result='SUCCESS' if revoked else 'NOOP')


def _apply_extend(ticket: PermissionApprovalTicket, actor: User):
    """延期（EXPIRE_EXTEND）—— 仅更新 expires_at，不改状态"""
    new_expires = ticket.expires_at
    updated = False
    for rel_qs in (
        UserRoleRel.objects.filter(user=ticket.target_user, role=ticket.role, status=GrantStatus.ACTIVE),
        UserDeptScopeRel.objects.filter(user=ticket.target_user, role=ticket.role, status=GrantStatus.ACTIVE),
        UserTeamScopeRel.objects.filter(user=ticket.target_user, role=ticket.role, status=GrantStatus.ACTIVE),
    ):
        if rel_qs.update(expires_at=new_expires, ticket=ticket):
            updated = True
    _write_audit(ticket, actor, 'EXPIRE_EXTEND', '', '',
                 result='SUCCESS' if updated else 'NOOP')


# ============================================================================
# 降级/撤销直接执行（绕过工单，团队组长可直接撤销，仅记审计）
# ============================================================================

@transaction.atomic
def revoke_direct(actor: User, target_user: User, role: Role,
                  scope_type: str = ScopeType.NONE, scope_id=None,
                  reason: str = '', ip_address: str = '', user_agent: str = '') -> PermissionApprovalTicket:
    """降级/撤销直接执行 —— 团队组长可直接撤销普通角色授权，无需审批

    适用场景（build_approval_chain 返回空链的场景）：
    - REVOKE 非 super_admin 角色：团队组长直接撤销本团队内授权
    - 不涉及 super_admin 角色的撤销

    仍创建工单留痕（status=EXECUTED），保证审计可追溯。
    super_admin 角色撤销不应走此入口，必须 create_ticket 走双审。
    """
    # 超管角色（super_admin）撤销必须走审批工单（双人复核）
    # 不能走 revoke_direct 绕过审批，否则单点撤销超管权限有安全风险
    if role and role.role_key in ('super_admin',):
        raise ValueError('超管角色撤销必须走审批工单（双人复核）')

    ticket = PermissionApprovalTicket.objects.create(
        ticket_no=_gen_ticket_no(),
        applicant=actor,
        target_user=target_user,
        change_type=TicketChangeType.REVOKE,
        role=role,
        scope_type=scope_type,
        scope_id=scope_id,
        reason=reason,
        approval_chain=[],
        current_step=0,
        status=TicketStatus.EXECUTED,
        approved_at=timezone.now(),
        executed_at=timezone.now(),
    )
    _apply_revoke(ticket, actor)
    _write_audit(ticket, actor, AuditAction.TICKET_CREATE, ip_address, user_agent, result='SUCCESS')
    _write_audit(ticket, actor, AuditAction.TICKET_EXECUTE, ip_address, user_agent, result='SUCCESS')
    logger.info(f'[Ticket] 直接撤销(无需审批): role={role.role_key if role else "-"} '
                f'target={target_user.id} by={actor.id}')
    return ticket


# ============================================================================
# 审计写入
# ============================================================================

def _write_audit(ticket: PermissionApprovalTicket, actor: User, action: str,
                 ip_address: str, user_agent: str, result: str = 'SUCCESS', extra: dict = None):
    """写权限审计日志 —— 工单全生命周期留痕

    target_type=TICKET，target_id=ticket.id，便于按工单反查所有审计事件。
    extra 合并到 after_snapshot，记录节点/评论等上下文。
    """
    after = {'ticket_no': ticket.ticket_no, 'change_type': ticket.change_type,
             'status': ticket.status}
    if extra:
        after.update(extra)
    PermissionAuditLog.objects.create(
        actor=actor,
        action=action,
        target_type=AuditTargetType.TICKET,
        target_id=ticket.id,
        target_user=ticket.target_user,
        role=ticket.role,
        scope_type=ticket.scope_type,
        scope_id=ticket.scope_id,
        after_snapshot=after,
        result=result,
        ip_address=ip_address or None,
        user_agent=user_agent or '',
    )
