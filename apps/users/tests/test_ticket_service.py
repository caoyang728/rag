"""
apps.users.ticket_service 集成测试 —— 权限配置审批工单服务

覆盖范围：
- create_ticket：工单创建（空链直接执行 / 非空链待审批 / SoD 互斥 / 超管配额）
- approve_ticket：审批通过（末节点 → APPROVED → EXECUTED；中间节点 → current_step+1）
- reject_ticket：驳回工单（PENDING → REJECTED 终态）
- cancel_ticket：发起人撤回（PENDING → CANCELLED 终态）
- _can_approve_for_role：共享审批池角色匹配 + 回避原则 + 双人独立性
- build_approval_chain：审批链构造矩阵（GRANT/REVOKE/ROLE_CHANGE × 角色类型）
- revoke_direct：降级/撤销直接执行（绕过审批，仅记审计）

工单流转状态机（核心断言目标）：
  PENDING --approve(末节点)--> APPROVED --execute--> EXECUTED
  PENDING --reject--> REJECTED（终态）
  PENDING --cancel(发起人)--> CANCELLED（终态）

采用 DB 集成测试：
工单流转涉及事务（@transaction.atomic）、多表写入（授权表 + 审计表 + 工单表）、
select_for_update 并发控制，纯 mock 无法验证状态机一致性与授权表真实写入。
"""
import pytest

from apps.users.models import (
    User, Role, Department, Team,
    UserRoleRel, UserDeptScopeRel, UserTeamScopeRel,
    PermissionApprovalTicket, PermissionAuditLog, RoleConflictRule,
    TicketStatus, TicketChangeType, ScopeType, RoleType, DataScope, GrantStatus,
    AuditTargetType,
)
from apps.users.ticket_service import (
    create_ticket, approve_ticket, reject_ticket, cancel_ticket, revoke_direct,
    build_approval_chain, _can_approve_for_role, ApproverRole,
    GLOBAL_HIGH_PRIVILEGE_KEYS, TEAM_ROLE_KEYS,
)


def _get_or_create_role(role_key, **defaults):
    """获取或创建内置角色，补齐默认字段"""
    default_map = {
        'super_admin': dict(name='超级管理员', role_type=RoleType.GLOBAL, data_scope=DataScope.GLOBAL, is_builtin=True),
        'user_admin': dict(name='用户管理员', role_type=RoleType.GLOBAL, data_scope=DataScope.GLOBAL, is_builtin=True),
        'kb_admin': dict(name='知识库管理员', role_type=RoleType.GLOBAL, data_scope=DataScope.GLOBAL, is_builtin=True),
        'compliance_admin': dict(name='合规管理员', role_type=RoleType.GLOBAL, data_scope=DataScope.GLOBAL, is_builtin=True),
        'dept_manager': dict(name='部门经理', role_type=RoleType.DEPT_SCOPE, data_scope=DataScope.DEPT, is_builtin=True),
        'team_leader': dict(name='团队组长', role_type=RoleType.TEAM_SCOPE, data_scope=DataScope.TEAM, is_builtin=True),
        'contributor': dict(name='贡献者', role_type=RoleType.NORMAL_USER, data_scope=DataScope.TEAM, is_builtin=True),
        'viewer': dict(name='查看者', role_type=RoleType.NORMAL_USER, data_scope=DataScope.TEAM, is_builtin=True),
    }
    defaults = {**default_map.get(role_key, {}), **defaults}
    role, _ = Role.objects.get_or_create(role_key=role_key, defaults=defaults)
    return role


def _grant_role(user, role_key):
    """给用户授予全局角色（status=ACTIVE），用于审批人身份准备"""
    role = _get_or_create_role(role_key)
    UserRoleRel.objects.update_or_create(
        user=user, role=role,
        defaults={'status': GrantStatus.ACTIVE},
    )
    return role


def _create_user(username, **extra):
    """创建测试用户"""
    return User.objects.create_user(
        username=username, email=f'{username}@test.com',
        password='pass12345', **extra)


@pytest.mark.django_db
class TicketTestBase:
    """工单测试公共基类 —— 准备组织架构、角色与审批人（子类自动继承 django_db）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入角色/部门/团队/审批人"""
        # 内置角色全部预建（审批链构造与授权写入依赖角色存在）
        for key in ['super_admin', 'user_admin', 'kb_admin', 'compliance_admin',
                    'dept_manager', 'team_leader', 'contributor', 'viewer']:
            _get_or_create_role(key)

        # 部门 + 2 个团队（各设组长），用于跨团队/跨部门审批链
        self.dept = Department.objects.create(name='研发部', code='rd')
        self.team1 = Team.objects.create(name='后端组', code='rd_backend', department=self.dept)
        self.team2 = Team.objects.create(name='前端组', code='rd_frontend', department=self.dept)

        # 审批人：团队组长（绑定到 Team.leader_id，_can_approve_for_role 据此匹配）
        self.team_leader1 = _create_user('leader1', team=self.team1, department=self.dept)
        self.team_leader2 = _create_user('leader2', team=self.team2, department=self.dept)
        self.team1.leader = self.team_leader1
        self.team1.save()
        self.team2.leader = self.team_leader2
        self.team2.save()
        # 组长需持有 team_leader 角色才满足审批身份（_can_approve_for_role 对 TEAM_LEADER 只看 team.leader_id，
        # 但 SoD/其他校验可能依赖角色，此处一并授予保持业务真实）
        _grant_role(self.team_leader1, 'team_leader')
        _grant_role(self.team_leader2, 'team_leader')

        # 部门经理（绑定到 Department.leader_id）
        self.dept_leader = _create_user('deptmgr', department=self.dept)
        self.dept.leader = self.dept_leader
        self.dept.save()
        _grant_role(self.dept_leader, 'dept_manager')

        # 3 个超管（满足双超管审批配额 ≥2，排除申请人后仍有 2 人）
        self.sa1 = _create_user('sa1')
        self.sa2 = _create_user('sa2')
        self.sa3 = _create_user('sa3')
        for sa in (self.sa1, self.sa2, self.sa3):
            _grant_role(sa, 'super_admin')

        # 普通申请人（归属 team1，无管理角色）
        self.applicant = _create_user('applicant', team=self.team1, department=self.dept)
        # 目标用户（无任何角色，作为授权对象）
        self.target_user = _create_user('target')

        self.viewer = Role.objects.get(role_key='viewer')
        self.contributor = Role.objects.get(role_key='contributor')


# ============================================================================
# create_ticket：工单创建
# ============================================================================
class TestCreateTicket(TicketTestBase):
    """create_ticket：空链直接执行 / 非空链待审批 / SoD 互斥 / 超管配额"""

    @pytest.mark.integration
    def test_revoke_contributor_direct_execution(self):
        """REVOKE contributor 审批链为空 → 直接执行，工单状态 EXECUTED（无需审批）"""
        # 先给目标用户授予 contributor（团队属地），作为撤销前提
        UserTeamScopeRel.objects.create(
            user=self.target_user, role=self.contributor,
            team=self.team2, status=GrantStatus.ACTIVE,
        )
        ticket = create_ticket(
            applicant=self.team_leader2, target_user=self.target_user,
            change_type=TicketChangeType.REVOKE, role=self.contributor,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
            reason='撤销贡献者',
        )
        # 空链 → 直接 EXECUTED
        assert ticket.status == TicketStatus.EXECUTED
        assert ticket.approval_chain == []
        assert ticket.executed_at is not None
        # 授权记录已被撤销
        assert not UserTeamScopeRel.objects.filter(
            user=self.target_user, role=self.contributor,
            team=self.team2, status=GrantStatus.ACTIVE,
        ).exists()

    @pytest.mark.integration
    def test_grant_viewer_cross_team_creates_pending_ticket(self):
        """GRANT viewer 跨团队 → 目标团队组长单审链 → 工单 PENDING"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
            reason='申请 viewer',
        )
        # 非空链 → PENDING
        assert ticket.status == TicketStatus.PENDING
        assert ticket.current_step == 0
        assert len(ticket.approval_chain) == 1
        # 目标团队组长单审
        assert ticket.approval_chain[0]['approver_role'] == ApproverRole.TEAM_LEADER
        assert ticket.approval_chain[0]['approver_scope_id'] == self.team2.id

    @pytest.mark.integration
    def test_sod_conflict_rejects_high_privilege_role(self):
        """SoD 互斥：已持有 super_admin 再申请 user_admin 应抛 ValueError"""
        # 目标用户先持有 super_admin
        _grant_role(self.target_user, 'super_admin')
        # 建立互斥规则（4 高权两两互斥）
        sa_role = Role.objects.get(role_key='super_admin')
        ua_role = Role.objects.get(role_key='user_admin')
        RoleConflictRule.objects.create(role_a=sa_role, role_b=ua_role, reason='4 高权 4 选 1')

        with pytest.raises(ValueError, match='SoD 互斥冲突'):
            create_ticket(
                applicant=self.sa1, target_user=self.target_user,
                change_type=TicketChangeType.GRANT, role=ua_role,
                scope_type=ScopeType.NONE, reason='申请 user_admin',
            )

    @pytest.mark.integration
    def test_super_admin_quota_insufficient_rejected(self):
        """超管配额不足（可用超管 <2）时拒绝创建超管工单，避免工单卡死"""
        # 只保留 1 个可用超管（排除申请人 sa1 后仅剩 0 个其他超管）
        # 此处用一个新的申请人（非超管）发起 super_admin 申请，排除申请人后可用超管=3≥2 通过
        # 改为：让 sa1 作为申请人，可用超管=sa2,sa3=2，应通过配额；
        #       再用 sa2 发起，排除 sa2 后可用 sa1,sa3=2 仍通过；
        # 这里测试配额不足：临时移除 sa2/sa3 的超管角色
        UserRoleRel.objects.filter(
            user__in=[self.sa2, self.sa3], role__role_key='super_admin',
        ).update(status=GrantStatus.REVOKED)
        # 现仅 sa1 是超管；sa1 作为申请人，排除后可用超管=0 <2
        with pytest.raises(ValueError, match='可用超级管理员不足'):
            create_ticket(
                applicant=self.sa1, target_user=self.target_user,
                change_type=TicketChangeType.GRANT,
                role=Role.objects.get(role_key='super_admin'),
                scope_type=ScopeType.NONE, reason='申请超管',
            )


# ============================================================================
# 状态机：PENDING --approve(末节点)--> APPROVED --> EXECUTED
# ============================================================================
class TestApproveTicketStateMachine(TicketTestBase):
    """approve_ticket：末节点通过 → APPROVED → EXECUTED；中间节点 → current_step+1"""

    @pytest.mark.integration
    def test_single_node_approve_executes_grant(self):
        """单节点链：审批通过 → APPROVED → EXECUTED，授权表写入 ACTIVE 记录"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
            reason='申请 viewer',
        )
        assert ticket.status == TicketStatus.PENDING

        # 目标团队组长（team2.leader）审批通过
        approved = approve_ticket(ticket, self.team_leader2, comment='同意')
        # 末节点 → APPROVED → EXECUTED
        assert approved.status == TicketStatus.EXECUTED
        assert approved.approved_at is not None
        assert approved.executed_at is not None
        # 授权表已写入 ACTIVE 记录
        assert UserTeamScopeRel.objects.filter(
            user=self.target_user, role=self.viewer,
            team=self.team2, status=GrantStatus.ACTIVE,
        ).exists()

    @pytest.mark.integration
    def test_multi_node_approve_advances_step(self):
        """多节点链：中间节点通过 → current_step+1，工单仍 PENDING"""
        # contributor 跨团队申请：本团队组长 → 目标团队组长 双审
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.contributor,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
            reason='申请 contributor 跨团队',
        )
        # 跨团队 contributor 应产生 2 节点链
        assert len(ticket.approval_chain) == 2
        assert ticket.current_step == 0

        # 第 1 节点：本团队组长（applicant 在 team1 → team1.leader）
        approved = approve_ticket(ticket, self.team_leader1, comment='本团队同意')
        # 中间节点 → 推进到下一节点，仍 PENDING
        assert approved.status == TicketStatus.PENDING
        assert approved.current_step == 1

        # 第 2 节点：目标团队组长（team2.leader）
        approved = approve_ticket(ticket, self.team_leader2, comment='目标团队同意')
        # 末节点 → EXECUTED
        assert approved.status == TicketStatus.EXECUTED
        assert UserTeamScopeRel.objects.filter(
            user=self.target_user, role=self.contributor,
            team=self.team2, status=GrantStatus.ACTIVE,
        ).exists()

    @pytest.mark.integration
    def test_approve_non_pending_raises(self):
        """非 PENDING 工单审批应抛 ValueError"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
        )
        approve_ticket(ticket, self.team_leader2)  # 通过 → EXECUTED
        # 已 EXECUTED 再审批应报错
        with pytest.raises(ValueError, match='工单非待审批状态'):
            approve_ticket(ticket, self.team_leader2)

    @pytest.mark.integration
    def test_approve_wrong_role_raises_permission_error(self):
        """审批人不具备当前节点 approver_role 应抛 PermissionError"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
        )
        # team1.leader 不是 team2 的组长，无权审批 team2 节点
        with pytest.raises(PermissionError):
            approve_ticket(ticket, self.team_leader1)

    @pytest.mark.integration
    def test_applicant_cannot_approve_own_ticket(self):
        """回避原则：申请人不能审批自己的工单"""
        # 让申请人恰好是目标团队组长（构造自审场景）
        self.team2.leader = self.applicant
        self.team2.save()
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
        )
        # 申请人审批自己的工单 → _can_approve_for_role 返回 False → PermissionError
        with pytest.raises(PermissionError):
            approve_ticket(ticket, self.applicant)


# ============================================================================
# 状态机：PENDING --reject--> REJECTED（终态）
# ============================================================================
class TestRejectTicket(TicketTestBase):
    """reject_ticket：驳回工单 → REJECTED 终态，不执行授权写入"""

    @pytest.mark.integration
    def test_reject_sets_rejected_status(self):
        """当前节点审批人驳回 → 工单 REJECTED，授权表不写入"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
        )
        rejected = reject_ticket(ticket, self.team_leader2, comment='不同意')
        assert rejected.status == TicketStatus.REJECTED
        # 驳回不执行授权写入
        assert not UserTeamScopeRel.objects.filter(
            user=self.target_user, role=self.viewer, team=self.team2,
        ).exists()
        # 审计日志记录驳回事件
        assert PermissionAuditLog.objects.filter(
            action='TICKET_REJECT', target_id=ticket.id,
        ).exists()

    @pytest.mark.integration
    def test_reject_non_pending_raises(self):
        """非 PENDING 工单驳回应抛 ValueError"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
        )
        reject_ticket(ticket, self.team_leader2)  # → REJECTED
        with pytest.raises(ValueError, match='工单非待审批状态'):
            reject_ticket(ticket, self.team_leader2)

    @pytest.mark.integration
    def test_super_admin_can_reject_any_ticket(self):
        """超管兜底越级驳回：即使非当前节点审批人也可驳回"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
        )
        # sa1 既非 team2 组长也非申请人，但 super_admin 可兜底驳回
        rejected = reject_ticket(ticket, self.sa1, comment='超管驳回')
        assert rejected.status == TicketStatus.REJECTED


# ============================================================================
# 状态机：PENDING --cancel(发起人)--> CANCELLED（终态）
# ============================================================================
class TestCancelTicket(TicketTestBase):
    """cancel_ticket：发起人撤回 → CANCELLED 终态"""

    @pytest.mark.integration
    def test_applicant_cancel_sets_cancelled(self):
        """发起人撤回 PENDING 工单 → CANCELLED"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
        )
        cancelled = cancel_ticket(ticket, self.applicant)
        assert cancelled.status == TicketStatus.CANCELLED

    @pytest.mark.integration
    def test_cancel_non_pending_raises(self):
        """非 PENDING 工单不可撤回（已执行不可撤，防止状态不一致）"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
        )
        approve_ticket(ticket, self.team_leader2)  # → EXECUTED
        # approve_ticket 内部 select_for_update 返回新对象，此处需刷新以读取终态
        ticket.refresh_from_db()
        with pytest.raises(ValueError, match='仅待审批工单可撤回'):
            cancel_ticket(ticket, self.applicant)

    @pytest.mark.integration
    def test_non_applicant_cannot_cancel(self):
        """非发起人且非超管不可撤回"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
        )
        with pytest.raises(PermissionError, match='仅发起人可撤回'):
            cancel_ticket(ticket, self.team_leader1)

    @pytest.mark.integration
    def test_super_admin_can_cancel_others_ticket(self):
        """超管可撤回他人工单（运维兜底）"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
        )
        cancelled = cancel_ticket(ticket, self.sa1)
        assert cancelled.status == TicketStatus.CANCELLED


# ============================================================================
# _can_approve_for_role：共享审批池角色匹配 + 排除规则
# ============================================================================
class TestCanApproveForRole(TicketTestBase):
    """_can_approve_for_role：审批人角色匹配 + 回避原则 + 双人独立性"""

    @pytest.mark.integration
    def test_super_admin_match(self):
        """持有 super_admin 角色的用户可审批 SUPER_ADMIN 节点"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
        )
        assert _can_approve_for_role(self.sa1, ApproverRole.SUPER_ADMIN, ticket) is True

    @pytest.mark.integration
    def test_non_super_admin_cannot_approve_super_admin_node(self):
        """非超管不可审批 SUPER_ADMIN 节点"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
        )
        assert _can_approve_for_role(self.team_leader1, ApproverRole.SUPER_ADMIN, ticket) is False

    @pytest.mark.integration
    def test_team_leader_match_by_scope(self):
        """TEAM_LEADER 节点：team.leader_id == user.id 且 scope 匹配才能审批"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
        )
        # team2.leader 可审批 team2 节点
        assert _can_approve_for_role(self.team_leader2, ApproverRole.TEAM_LEADER, ticket) is True
        # team1.leader 不可审批 team2 节点（scope 不匹配）
        assert _can_approve_for_role(self.team_leader1, ApproverRole.TEAM_LEADER, ticket) is False

    @pytest.mark.integration
    def test_applicant_excluded(self):
        """回避原则：申请人不能审批自己的工单"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
        )
        # applicant 不是超管，正常情况会返回 False；即使让其成为 team2.leader 也应被排除
        self.team2.leader = self.applicant
        self.team2.save()
        assert _can_approve_for_role(self.applicant, ApproverRole.TEAM_LEADER, ticket) is False

    @pytest.mark.integration
    def test_target_user_excluded(self):
        """回避原则：目标用户不能审批涉自己的工单"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
        )
        # target_user 即便持有 super_admin 也不能审（涉自己）
        _grant_role(self.target_user, 'super_admin')
        assert _can_approve_for_role(self.target_user, ApproverRole.SUPER_ADMIN, ticket) is False

    @pytest.mark.integration
    def test_dual_independence_prior_approver_excluded(self):
        """双人独立性：已审过前序节点的人不能再审后续节点（防双审变单审）"""
        # 构造双超管审批链：用 sa1 申请 user_admin（需双超管审批）
        # 先确保互斥规则不阻断（target_user 无高权角色）
        ticket = create_ticket(
            applicant=self.sa1, target_user=self.target_user,
            change_type=TicketChangeType.GRANT,
            role=Role.objects.get(role_key='user_admin'),
            scope_type=ScopeType.NONE, reason='申请 user_admin',
        )
        # 双超管链
        assert len(ticket.approval_chain) == 2
        # sa2 审批第 1 节点
        approve_ticket(ticket, self.sa2)
        # approve_ticket 内部 select_for_update 返回新对象，刷新以读取最新 chain/step
        ticket.refresh_from_db()
        # sa2 不能再审第 2 节点（双人独立性：前序节点 approver_id 已回填 sa2）
        assert _can_approve_for_role(self.sa2, ApproverRole.SUPER_ADMIN, ticket) is False
        # sa3 可以审第 2 节点
        assert _can_approve_for_role(self.sa3, ApproverRole.SUPER_ADMIN, ticket) is True


# ============================================================================
# build_approval_chain：审批链构造矩阵
# ============================================================================
class TestBuildApprovalChain(TicketTestBase):
    """build_approval_chain：按角色×场景×操作类型构造审批链"""

    @pytest.mark.integration
    def test_revoke_viewer_returns_empty_chain(self):
        """REVOKE viewer 跨团队 → 空链（直接执行，无需审批）"""
        chain = build_approval_chain(
            self.applicant, self.target_user, TicketChangeType.REVOKE,
            self.viewer, ScopeType.TEAM, self.team2.id,
        )
        assert chain == []

    @pytest.mark.integration
    def test_revoke_contributor_returns_empty_chain(self):
        """REVOKE contributor 任意 → 空链（直接执行）"""
        chain = build_approval_chain(
            self.applicant, self.target_user, TicketChangeType.REVOKE,
            self.contributor, ScopeType.TEAM, self.team2.id,
        )
        assert chain == []

    @pytest.mark.integration
    def test_grant_viewer_cross_team_single_node(self):
        """GRANT viewer 跨团队 → 目标团队组长单审"""
        chain = build_approval_chain(
            self.applicant, self.target_user, TicketChangeType.GRANT,
            self.viewer, ScopeType.TEAM, self.team2.id,
        )
        assert len(chain) == 1
        assert chain[0]['approver_role'] == ApproverRole.TEAM_LEADER
        assert chain[0]['approver_scope_id'] == self.team2.id

    @pytest.mark.integration
    def test_grant_contributor_cross_team_two_nodes(self):
        """GRANT contributor 跨团队 → 本团队组长 → 目标团队组长 双审"""
        chain = build_approval_chain(
            self.applicant, self.target_user, TicketChangeType.GRANT,
            self.contributor, ScopeType.TEAM, self.team2.id,
        )
        assert len(chain) == 2
        # 节点1：本团队组长（applicant 在 team1）
        assert chain[0]['approver_role'] == ApproverRole.TEAM_LEADER
        assert chain[0]['approver_scope_id'] == self.team1.id
        # 节点2：目标团队组长
        assert chain[1]['approver_role'] == ApproverRole.TEAM_LEADER
        assert chain[1]['approver_scope_id'] == self.team2.id

    @pytest.mark.integration
    def test_grant_super_admin_two_super_admin_nodes(self):
        """GRANT super_admin → 双超管审批链（强制双人独立）"""
        chain = build_approval_chain(
            self.sa1, self.target_user, TicketChangeType.GRANT,
            Role.objects.get(role_key='super_admin'),
            ScopeType.NONE, None,
        )
        assert len(chain) == 2
        assert all(n['approver_role'] == ApproverRole.SUPER_ADMIN for n in chain)

    @pytest.mark.integration
    def test_grant_kb_admin_user_admin_then_super(self):
        """GRANT kb_admin → 用户管理员 → 超管 双轨"""
        chain = build_approval_chain(
            self.applicant, self.target_user, TicketChangeType.GRANT,
            Role.objects.get(role_key='kb_admin'),
            ScopeType.NONE, None,
        )
        assert len(chain) == 2
        assert chain[0]['approver_role'] == ApproverRole.USER_ADMIN
        assert chain[1]['approver_role'] == ApproverRole.SUPER_ADMIN


# ============================================================================
# revoke_direct：降级/撤销直接执行（绕过审批，仅记审计）
# ============================================================================
class TestRevokeDirect(TicketTestBase):
    """revoke_direct：团队组长可直接撤销普通角色，无需审批"""

    @pytest.mark.integration
    def test_revoke_direct_executes_immediately(self):
        """直接撤销 contributor → 工单 EXECUTED，授权记录置 REVOKED"""
        UserTeamScopeRel.objects.create(
            user=self.target_user, role=self.contributor,
            team=self.team2, status=GrantStatus.ACTIVE,
        )
        ticket = revoke_direct(
            actor=self.team_leader2, target_user=self.target_user,
            role=self.contributor,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
            reason='直接撤销',
        )
        assert ticket.status == TicketStatus.EXECUTED
        assert ticket.approval_chain == []
        # 授权记录已撤销
        rel = UserTeamScopeRel.objects.filter(
            user=self.target_user, role=self.contributor, team=self.team2,
        ).first()
        assert rel.status == GrantStatus.REVOKED

    @pytest.mark.integration
    def test_revoke_direct_super_admin_rejected(self):
        """超管角色撤销必须走审批工单，revoke_direct 应拒绝"""
        with pytest.raises(ValueError, match='超管角色撤销必须走审批工单'):
            revoke_direct(
                actor=self.sa1, target_user=self.target_user,
                role=Role.objects.get(role_key='super_admin'),
                scope_type=ScopeType.NONE,
            )
