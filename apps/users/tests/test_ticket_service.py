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
from unittest.mock import patch

from apps.users.models import (
    User, Role, Department, Team,
    UserRoleRel, UserDeptScopeRel, UserTeamScopeRel,
    TicketList, TicketPermissionDetail, PermissionAuditLog, RoleConflictRule,
    TicketStatus, TicketChangeType, ScopeType, RoleType, DataScope, GrantStatus,
    AuditTargetType, TicketBizType,
)
from apps.users.ticket_service import (
    create_ticket, approve_ticket, reject_ticket, cancel_ticket, revoke_direct,
    build_approval_chain, _can_approve_for_role, _apply_grant, _apply_revoke,
    _apply_extend, _build_chain_node, _execute_grant_or_revoke, _sync_leader_for_role,
    _apply_role_change,
    parse_change_summary, _find_approver_ids_for_role, _get_team_leader_id,
    _get_dept_leader_id, _get_super_admin_ids, _check_sod_conflict,
    _detect_team_role_in_service, _detect_dept_role_in_service,
    _resolve_team_leader, _build_grant_chain_for_team_role,
    _build_revoke_chain_for_team_role, _is_same_team, _get_team_dept_id,
    _gen_ticket_no, _resolve_dept_leader,
    ApproverRole, GLOBAL_HIGH_PRIVILEGE_KEYS, TEAM_ROLE_KEYS,
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


def _create_approved_ticket(applicant, target_user, role, scope_type, scope_id, ticket_no):
    """构造已 APPROVED 的统一工单（主表 + 权限详情子表）—— 供 _apply_grant 执行测试用

    统一工单下 target_user/role/scope 等业务字段由详情子表承载（_pd 代理读取），
    与生产创建路径（_create_permission_ticket）保持一致，仅跳过审批链直接置 APPROVED。
    """
    ticket = TicketList.objects.create(
        ticket_no=ticket_no, title='测试执行工单', biz_type=TicketBizType.PERMISSION,
        applicant=applicant, status=TicketStatus.APPROVED,
        approval_chain=[], current_step=0,
    )
    TicketPermissionDetail.objects.create(
        ticket=ticket, target_user=target_user, change_type=TicketChangeType.GRANT,
        role=role, scope_type=scope_type, scope_id=scope_id, reason='',
    )
    return ticket


def _make_pending_ticket(applicant, target_user, role, chain,
                         scope_type=ScopeType.NONE, scope_id=None,
                         change_type=TicketChangeType.GRANT, ticket_no=None):
    """构造 PENDING 待审批工单（可指定自定义审批链）—— 供审批/驳回分支测试使用"""
    import time as _t
    ticket = TicketList.objects.create(
        ticket_no=ticket_no or f'TP-{_t.time_ns() % 10**8}',
        title='测试待审批工单', biz_type=TicketBizType.PERMISSION,
        applicant=applicant, status=TicketStatus.PENDING,
        approval_chain=chain, current_step=0,
    )
    TicketPermissionDetail.objects.create(
        ticket=ticket, target_user=target_user, change_type=change_type,
        role=role, scope_type=scope_type, scope_id=scope_id, reason='',
    )
    return ticket


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

        # 跨部门场景：市场部 + 市场一组（用于构造跨部门 PENDING 工单）
        self.dept2 = Department.objects.create(name='市场部', code='mkt')
        self.team3 = Team.objects.create(name='市场一组', code='mkt_1', department=self.dept2)
        self.dept2_leader = _create_user('dept2mgr', department=self.dept2)
        self.dept2.leader = self.dept2_leader
        self.dept2.save()
        # 市场部经理通过工单授予路径(UserDeptScopeRel 主源)授权
        UserDeptScopeRel.objects.create(
            user=self.dept2_leader, role=Role.objects.get(role_key='dept_manager'),
            dept=self.dept2, status=GrantStatus.ACTIVE,
        )

        # 用户管理员(USER_ADMIN 审批池,2 人:申请人排除后仍有另一人可审) + 文档管理员(KB_ADMIN 审批池)
        self.user_admin1 = _create_user('ua1')
        _grant_role(self.user_admin1, 'user_admin')
        self.user_admin2 = _create_user('ua2')
        _grant_role(self.user_admin2, 'user_admin')
        self.kb_admin1 = _create_user('kba1')
        _grant_role(self.kb_admin1, 'kb_admin')

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
    def test_grant_viewer_cross_dept_creates_pending_ticket(self):
        """GRANT viewer 跨部门团队 → 资源部门经理单审链 → 工单 PENDING"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team3.id,
            reason='申请 viewer',
        )
        # 非空链 → PENDING
        assert ticket.status == TicketStatus.PENDING
        assert ticket.current_step == 0
        assert len(ticket.approval_chain) == 1
        # 跨部门团队:资源部门经理(市场部)单审
        assert ticket.approval_chain[0]['approver_role'] == ApproverRole.DEPT_LEADER
        assert ticket.approval_chain[0]['approver_scope_id'] == self.dept2.id

    @pytest.mark.integration
    def test_grant_viewer_same_dept_cross_team_direct_execution(self):
        """GRANT viewer 本部门其他团队 → 组长提单自动生效(空链直接执行)"""
        ticket = create_ticket(
            applicant=self.team_leader2, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team2.id,
            reason='组长提单 viewer',
        )
        assert ticket.status == TicketStatus.EXECUTED
        assert ticket.approval_chain == []
        # 授权表已写入 ACTIVE 记录
        assert UserTeamScopeRel.objects.filter(
            user=self.target_user, role=self.viewer,
            team=self.team2, status=GrantStatus.ACTIVE,
        ).exists()

    @pytest.mark.integration
    def test_grant_contributor_same_team_direct_execution(self):
        """GRANT contributor 本团队 → 组长提单自动生效(空链直接执行)"""
        ticket = create_ticket(
            applicant=self.team_leader1, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.contributor,
            scope_type=ScopeType.TEAM, scope_id=self.team1.id,
            reason='组长提单 contributor',
        )
        assert ticket.status == TicketStatus.EXECUTED
        assert ticket.approval_chain == []
        assert UserTeamScopeRel.objects.filter(
            user=self.target_user, role=self.contributor,
            team=self.team1, status=GrantStatus.ACTIVE,
        ).exists()

    @pytest.mark.integration
    def test_grant_dept_contributor_same_dept_direct_execution(self):
        """GRANT contributor 部门级本部门 → 部门经理提单自动生效(空链直接执行)"""
        ticket = create_ticket(
            applicant=self.dept_leader, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.contributor,
            scope_type=ScopeType.DEPT, scope_id=self.dept.id,
            reason='部门经理提单 contributor',
        )
        assert ticket.status == TicketStatus.EXECUTED
        assert ticket.approval_chain == []
        assert UserDeptScopeRel.objects.filter(
            user=self.target_user, role=self.contributor,
            dept=self.dept, status=GrantStatus.ACTIVE,
        ).exists()

    @pytest.mark.integration
    def test_grant_dept_contributor_cross_dept_creates_pending_ticket(self):
        """GRANT contributor 部门级跨部门 → 文档管理员(kb_admin)审核 → 工单 PENDING"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.contributor,
            scope_type=ScopeType.DEPT, scope_id=self.dept2.id,
            reason='申请部门级 contributor',
        )
        assert ticket.status == TicketStatus.PENDING
        assert len(ticket.approval_chain) == 1
        assert ticket.approval_chain[0]['approver_role'] == ApproverRole.KB_ADMIN

    @pytest.mark.integration
    def test_grant_contributor_role_change_same_team_direct_execution(self):
        """本团队 viewer→contributor 角色变更:组长提单自动生效,原子撤销旧角色"""
        # 目标用户先持有 viewer(团队属地),组长提单升 contributor
        UserTeamScopeRel.objects.create(
            user=self.target_user, role=self.viewer,
            team=self.team1, status=GrantStatus.ACTIVE,
        )
        ticket = create_ticket(
            applicant=self.team_leader1, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.contributor,
            scope_type=ScopeType.TEAM, scope_id=self.team1.id,
            reason='组长提单升权',
        )
        # 互斥自动转 ROLE_CHANGE + 空链直接执行
        assert ticket.status == TicketStatus.EXECUTED
        assert ticket.change_type == TicketChangeType.ROLE_CHANGE
        # 旧 viewer 已撤销,新 contributor 已授予
        assert not UserTeamScopeRel.objects.filter(
            user=self.target_user, role=self.viewer,
            team=self.team1, status=GrantStatus.ACTIVE,
        ).exists()
        assert UserTeamScopeRel.objects.filter(
            user=self.target_user, role=self.contributor,
            team=self.team1, status=GrantStatus.ACTIVE,
        ).exists()

    @pytest.mark.integration
    def test_duplicate_pending_ticket_rejected(self):
        """工单防重:同目标用户同 scope 已有 PENDING 工单时拒绝重复提交"""
        # 先创建跨部门 viewer 工单(PENDING)
        create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team3.id,
            reason='申请 viewer',
        )
        with pytest.raises(ValueError, match='已有待审批的授权工单'):
            create_ticket(
                applicant=self.applicant, target_user=self.target_user,
                change_type=TicketChangeType.GRANT, role=self.viewer,
                scope_type=ScopeType.TEAM, scope_id=self.team3.id,
                reason='重复申请 viewer',
            )

    @pytest.mark.integration
    def test_grant_team_leader_when_team_has_leader_then_rejected(self):
        """管理岗名额唯一：团队已有组长时任命他人 → ValueError（需先撤销现任）"""
        tl_role = Role.objects.get(role_key='team_leader')
        with pytest.raises(ValueError, match='该团队已有组长'):
            create_ticket(
                applicant=self.sa1, target_user=self.target_user,
                change_type=TicketChangeType.GRANT, role=tl_role,
                scope_type=ScopeType.TEAM, scope_id=self.team1.id,
                reason='任命组长',
            )

    @pytest.mark.integration
    def test_grant_dept_manager_when_dept_has_manager_then_rejected(self):
        """管理岗名额唯一：部门已有经理时任命他人 → ValueError（需先撤销现任）"""
        dm_role = Role.objects.get(role_key='dept_manager')
        with pytest.raises(ValueError, match='该部门已有经理'):
            create_ticket(
                applicant=self.sa1, target_user=self.target_user,
                change_type=TicketChangeType.GRANT, role=dm_role,
                scope_type=ScopeType.DEPT, scope_id=self.dept.id,
                reason='任命经理',
            )

    @pytest.mark.integration
    def test_grant_team_leader_to_existing_leader_self_renew_then_allowed(self):
        """现任组长本人续期 → 不拦截（名额唯一仅限他人，本人可续期/变更）"""
        tl_role = Role.objects.get(role_key='team_leader')
        ticket = create_ticket(
            applicant=self.sa1, target_user=self.team_leader1,
            change_type=TicketChangeType.GRANT, role=tl_role,
            scope_type=ScopeType.TEAM, scope_id=self.team1.id,
            reason='组长续期',
        )
        # 非空链 → PENDING（user_admin 单审），未被名额唯一校验拦截
        assert ticket.status == TicketStatus.PENDING
        assert ticket.approval_chain[0]['approver_role'] == ApproverRole.USER_ADMIN

    @pytest.mark.integration
    def test_grant_team_leader_when_leader_via_team_rel_then_rejected(self):
        """leader_id 缺失但授权表有活跃 team_leader → 同样拒绝（双来源兜底判定）"""
        # 新建无 leader_id 的团队,但已有他人的活跃 team_leader 授权
        teamY = Team.objects.create(name='备用组', code='spare', department=self.dept)
        other = _create_user('other_leader', team=teamY, department=self.dept)
        UserTeamScopeRel.objects.create(
            user=other, role=Role.objects.get(role_key='team_leader'),
            team=teamY, status=GrantStatus.ACTIVE,
        )
        tl_role = Role.objects.get(role_key='team_leader')
        with pytest.raises(ValueError, match='该团队已有组长'):
            create_ticket(
                applicant=self.sa1, target_user=self.target_user,
                change_type=TicketChangeType.GRANT, role=tl_role,
                scope_type=ScopeType.TEAM, scope_id=teamY.id,
                reason='任命组长',
            )

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
        # 跨部门团队 viewer：资源部门经理(市场部)单审节点
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team3.id,
            reason='申请 viewer',
        )
        assert ticket.status == TicketStatus.PENDING

        # 资源部门经理（dept2.leader）审批通过
        approved = approve_ticket(ticket, self.dept2_leader, comment='同意')
        # 末节点 → APPROVED → EXECUTED
        assert approved.status == TicketStatus.EXECUTED
        assert approved.approved_at is not None
        assert approved.executed_at is not None
        # 授权表已写入 ACTIVE 记录
        assert UserTeamScopeRel.objects.filter(
            user=self.target_user, role=self.viewer,
            team=self.team3, status=GrantStatus.ACTIVE,
        ).exists()

    @pytest.mark.integration
    def test_multi_node_approve_advances_step(self):
        """多节点链：中间节点通过 → current_step+1，工单仍 PENDING"""
        # dept_manager 授权链：用户管理员 → 超管 双节点（人员管理发起,另一人员管理审批,超管复核）
        # 新建空部门作为任命目标：setup 中 dept/dept2 均已预置经理，避开"已有经理"唯一性校验
        dept3 = Department.objects.create(name='项目部', code='proj')
        ticket = create_ticket(
            applicant=self.sa1, target_user=self.target_user,
            change_type=TicketChangeType.GRANT,
            role=Role.objects.get(role_key='dept_manager'),
            scope_type=ScopeType.DEPT, scope_id=dept3.id,
            reason='任命部门经理',
        )
        # 应产生 2 节点链
        assert len(ticket.approval_chain) == 2
        assert ticket.current_step == 0

        # 第 1 节点：用户管理员（user_admin1）
        approved = approve_ticket(ticket, self.user_admin1, comment='人员管理同意')
        # 中间节点 → 推进到下一节点，仍 PENDING
        assert approved.status == TicketStatus.PENDING
        assert approved.current_step == 1

        # 第 2 节点：超管（sa2，与节点1 审批人不同 → 双人独立性）
        approved = approve_ticket(ticket, self.sa2, comment='超管复核')
        # 末节点 → EXECUTED
        assert approved.status == TicketStatus.EXECUTED
        assert UserDeptScopeRel.objects.filter(
            user=self.target_user, role=Role.objects.get(role_key='dept_manager'),
            dept=dept3, status=GrantStatus.ACTIVE,
        ).exists()

    @pytest.mark.integration
    def test_approve_non_pending_raises(self):
        """非 PENDING 工单审批应抛 ValueError"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team3.id,
        )
        approve_ticket(ticket, self.dept2_leader)  # 通过 → EXECUTED
        # 已 EXECUTED 再审批应报错
        with pytest.raises(ValueError, match='工单非待审批状态'):
            approve_ticket(ticket, self.dept2_leader)

    @pytest.mark.integration
    def test_approve_wrong_role_raises_permission_error(self):
        """审批人不具备当前节点 approver_role 应抛 PermissionError"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team3.id,
        )
        # 当前节点是市场部经理(DEPT_LEADER dept2)，研发部组长无权审批
        with pytest.raises(PermissionError):
            approve_ticket(ticket, self.team_leader1)

    @pytest.mark.integration
    def test_applicant_cannot_approve_own_ticket(self):
        """回避原则：申请人不能审批自己的工单"""
        # 申请人为文档管理员，提交部门级跨部门 viewer 授权（KB_ADMIN 审核节点）
        ticket = create_ticket(
            applicant=self.kb_admin1, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.DEPT, scope_id=self.dept2.id,
        )
        assert ticket.status == TicketStatus.PENDING
        assert ticket.approval_chain[0]['approver_role'] == ApproverRole.KB_ADMIN
        # 申请人虽持有 kb_admin 角色，但回避原则(闸 1)拒绝其审批自己的工单
        with pytest.raises(PermissionError):
            approve_ticket(ticket, self.kb_admin1)


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
            scope_type=ScopeType.TEAM, scope_id=self.team3.id,
        )
        rejected = reject_ticket(ticket, self.dept2_leader, comment='不同意')
        assert rejected.status == TicketStatus.REJECTED
        # 驳回不执行授权写入
        assert not UserTeamScopeRel.objects.filter(
            user=self.target_user, role=self.viewer, team=self.team3,
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
            scope_type=ScopeType.TEAM, scope_id=self.team3.id,
        )
        reject_ticket(ticket, self.dept2_leader)  # → REJECTED
        with pytest.raises(ValueError, match='工单非待审批状态'):
            reject_ticket(ticket, self.dept2_leader)

    @pytest.mark.integration
    def test_super_admin_can_reject_any_ticket(self):
        """超管兜底越级驳回：即使非当前节点审批人也可驳回"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team3.id,
        )
        # sa1 既非市场部经理也非申请人，但 super_admin 可兜底驳回
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
            scope_type=ScopeType.TEAM, scope_id=self.team3.id,
        )
        cancelled = cancel_ticket(ticket, self.applicant)
        assert cancelled.status == TicketStatus.CANCELLED

    @pytest.mark.integration
    def test_cancel_non_pending_raises(self):
        """非 PENDING 工单不可撤回（已执行不可撤，防止状态不一致）"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team3.id,
        )
        approve_ticket(ticket, self.dept2_leader)  # → EXECUTED
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
            scope_type=ScopeType.TEAM, scope_id=self.team3.id,
        )
        with pytest.raises(PermissionError, match='仅发起人可撤回'):
            cancel_ticket(ticket, self.team_leader1)

    @pytest.mark.integration
    def test_super_admin_can_cancel_others_ticket(self):
        """超管可撤回他人工单（运维兜底）"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team3.id,
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
            scope_type=ScopeType.TEAM, scope_id=self.team3.id,
        )
        assert _can_approve_for_role(self.sa1, ApproverRole.SUPER_ADMIN, ticket) is True

    @pytest.mark.integration
    def test_non_super_admin_cannot_approve_super_admin_node(self):
        """非超管不可审批 SUPER_ADMIN 节点"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team3.id,
        )
        assert _can_approve_for_role(self.team_leader1, ApproverRole.SUPER_ADMIN, ticket) is False

    @pytest.mark.integration
    def test_team_leader_match_by_scope(self):
        """TEAM_LEADER 节点：team.leader_id == user.id 且 scope 匹配才能审批(兼容历史工单)"""
        # 新矩阵下 GRANT 不再产生 TEAM_LEADER 节点,但历史工单的 TEAM_LEADER 节点仍需支持审批,
        # 手工构造 TEAM_LEADER 节点工单验证判定逻辑
        ticket = TicketList(
            applicant_id=self.applicant.id,
            approval_chain=[_build_chain_node(ApproverRole.TEAM_LEADER,
                                              ScopeType.TEAM, self.team2.id)],
            current_step=0,
        )
        # team2.leader 可审批 team2 节点
        assert _can_approve_for_role(self.team_leader2, ApproverRole.TEAM_LEADER, ticket) is True
        # team1.leader 不可审批 team2 节点（scope 不匹配）
        assert _can_approve_for_role(self.team_leader1, ApproverRole.TEAM_LEADER, ticket) is False

    @pytest.mark.integration
    def test_kb_admin_match(self):
        """KB_ADMIN 节点：持有 kb_admin 角色的用户可审批,其他用户不可"""
        ticket = TicketList(
            applicant_id=self.applicant.id,
            approval_chain=[_build_chain_node(ApproverRole.KB_ADMIN)],
            current_step=0,
        )
        assert _can_approve_for_role(self.kb_admin1, ApproverRole.KB_ADMIN, ticket) is True
        # 非 kb_admin（team_leader）不可审
        assert _can_approve_for_role(self.team_leader1, ApproverRole.KB_ADMIN, ticket) is False

    @pytest.mark.integration
    def test_applicant_excluded(self):
        """回避原则：申请人不能审批自己的工单"""
        # 申请人为市场部经理,当前节点恰好也是市场部经理(DEPT_LEADER dept2)
        ticket = create_ticket(
            applicant=self.dept2_leader, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team3.id,
        )
        # 申请人(市场部经理)即便满足节点角色也被回避原则排除
        assert _can_approve_for_role(self.dept2_leader, ApproverRole.DEPT_LEADER, ticket) is False
        # 非申请人的市场部经理不存在;换用超管验证不误伤(非申请人可审)
        assert _can_approve_for_role(self.sa1, ApproverRole.SUPER_ADMIN, ticket) is True

    @pytest.mark.integration
    def test_target_user_excluded(self):
        """回避原则：目标用户不能审批涉自己的工单"""
        ticket = create_ticket(
            applicant=self.applicant, target_user=self.target_user,
            change_type=TicketChangeType.GRANT, role=self.viewer,
            scope_type=ScopeType.TEAM, scope_id=self.team3.id,
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
    def test_grant_viewer_cross_dept_single_node(self):
        """GRANT viewer 跨部门团队 → 资源部门经理单审"""
        chain = build_approval_chain(
            self.applicant, self.target_user, TicketChangeType.GRANT,
            self.viewer, ScopeType.TEAM, self.team3.id,
        )
        assert len(chain) == 1
        assert chain[0]['approver_role'] == ApproverRole.DEPT_LEADER
        assert chain[0]['approver_scope_id'] == self.dept2.id

    @pytest.mark.integration
    def test_grant_viewer_same_dept_cross_team_empty_chain(self):
        """GRANT viewer 本部门其他团队 → 空链(组长提单自动生效)"""
        chain = build_approval_chain(
            self.applicant, self.target_user, TicketChangeType.GRANT,
            self.viewer, ScopeType.TEAM, self.team2.id,
        )
        assert chain == []

    @pytest.mark.integration
    def test_grant_contributor_same_team_empty_chain(self):
        """GRANT contributor 本团队 → 空链(组长提单自动生效)"""
        chain = build_approval_chain(
            self.applicant, self.target_user, TicketChangeType.GRANT,
            self.contributor, ScopeType.TEAM, self.team1.id,
        )
        assert chain == []

    @pytest.mark.integration
    def test_grant_contributor_cross_dept_single_node(self):
        """GRANT contributor 跨部门团队 → 资源部门经理单审"""
        chain = build_approval_chain(
            self.applicant, self.target_user, TicketChangeType.GRANT,
            self.contributor, ScopeType.TEAM, self.team3.id,
        )
        assert len(chain) == 1
        assert chain[0]['approver_role'] == ApproverRole.DEPT_LEADER
        assert chain[0]['approver_scope_id'] == self.dept2.id

    @pytest.mark.integration
    def test_grant_dept_contributor_same_dept_empty_chain(self):
        """GRANT contributor 部门级本部门 → 空链(部门经理提单自动生效)"""
        chain = build_approval_chain(
            self.dept_leader, self.target_user, TicketChangeType.GRANT,
            self.contributor, ScopeType.DEPT, self.dept.id,
        )
        assert chain == []

    @pytest.mark.integration
    def test_grant_dept_contributor_cross_dept_kb_admin(self):
        """GRANT contributor 部门级跨部门 → 文档管理员(kb_admin)单审"""
        chain = build_approval_chain(
            self.applicant, self.target_user, TicketChangeType.GRANT,
            self.contributor, ScopeType.DEPT, self.dept2.id,
        )
        assert len(chain) == 1
        assert chain[0]['approver_role'] == ApproverRole.KB_ADMIN
        # 文档管理员可审批该节点
        assert _can_approve_for_role(self.kb_admin1, ApproverRole.KB_ADMIN,
                                     TicketList(
                                         approval_chain=chain, current_step=0,
                                         applicant_id=self.applicant.id,
                                     )) is True

    @pytest.mark.integration
    def test_revoke_dept_contributor_returns_empty_chain(self):
        """REVOKE contributor 部门级 → 空链(直接执行)"""
        chain = build_approval_chain(
            self.applicant, self.target_user, TicketChangeType.REVOKE,
            self.contributor, ScopeType.DEPT, self.dept2.id,
        )
        assert chain == []

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

    @pytest.mark.integration
    def test_grant_team_leader_user_admin_single_node(self):
        """GRANT team_leader → 用户管理员单审(部门经理发起,人员管理审批)"""
        chain = build_approval_chain(
            self.applicant, self.target_user, TicketChangeType.GRANT,
            Role.objects.get(role_key='team_leader'),
            ScopeType.TEAM, self.team1.id,
        )
        assert len(chain) == 1
        assert chain[0]['approver_role'] == ApproverRole.USER_ADMIN
        # 用户管理员可审批该节点
        assert _can_approve_for_role(self.user_admin1, ApproverRole.USER_ADMIN,
                                     TicketList(
                                         approval_chain=chain, current_step=0,
                                         applicant_id=self.applicant.id,
                                     )) is True
        # 部门经理(dept_manager)不再直接审批组长任命(定稿:人员管理审批)
        assert _can_approve_for_role(self.dept_leader, ApproverRole.USER_ADMIN,
                                     TicketList(
                                         approval_chain=chain, current_step=0,
                                         applicant_id=self.applicant.id,
                                     )) is False

    @pytest.mark.integration
    def test_grant_team_leader_applicant_is_user_admin_single_node(self):
        """GRANT team_leader 发起人为用户管理员 → 仍单节点(由另一 user_admin 审批,回避原则)"""
        chain = build_approval_chain(
            self.user_admin1, self.target_user, TicketChangeType.GRANT,
            Role.objects.get(role_key='team_leader'),
            ScopeType.TEAM, self.team1.id,
        )
        assert len(chain) == 1
        assert chain[0]['approver_role'] == ApproverRole.USER_ADMIN

    @pytest.mark.integration
    def test_grant_team_leader_wrong_scope_falls_back_super_admin(self):
        """GRANT team_leader 错误 scope_type(DEPT) → 兜底超管单审(保持兜底行为)"""
        chain = build_approval_chain(
            self.applicant, self.target_user, TicketChangeType.GRANT,
            Role.objects.get(role_key='team_leader'),
            ScopeType.DEPT, self.dept.id,
        )
        assert len(chain) == 1
        assert chain[0]['approver_role'] == ApproverRole.SUPER_ADMIN

    @pytest.mark.integration
    def test_grant_team_leader_team_missing_falls_back_super_admin(self):
        """GRANT team_leader 目标团队不存在 → 兜底超管单审"""
        chain = build_approval_chain(
            self.applicant, self.target_user, TicketChangeType.GRANT,
            Role.objects.get(role_key='team_leader'),
            ScopeType.TEAM, 99999999,
        )
        assert len(chain) == 1
        assert chain[0]['approver_role'] == ApproverRole.SUPER_ADMIN


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


# ============================================================================
# leader_id 同步：工单执行时维护 Team.leader_id / Department.leader_id
# ============================================================================
class TestGrantLeaderSync(TicketTestBase):
    """工单执行时同步组织 leader_id(team_leader → Team.leader, dept_manager → Department.leader)"""

    @pytest.mark.integration
    def test_grant_team_leader_syncs_team_leader_id(self):
        """授予 team_leader(团队无组长) → Team.leader_id 同步为被授权者"""
        teamX = Team.objects.create(name='空组长组', code='no_leader', department=self.dept)
        tl_role = Role.objects.get(role_key='team_leader')
        ticket = _create_approved_ticket(
            self.sa1, self.applicant, tl_role,
            ScopeType.TEAM, teamX.id, ticket_no='T-SYNC-1',
        )
        _apply_grant(ticket, actor=self.sa1)
        teamX.refresh_from_db()
        assert teamX.leader_id == self.applicant.id

    @pytest.mark.integration
    def test_grant_dept_manager_syncs_dept_leader_id(self):
        """授予 dept_manager(部门无经理) → Department.leader_id 同步为被授权者"""
        deptY = Department.objects.create(name='新部门', code='new_dept')
        dm_role = Role.objects.get(role_key='dept_manager')
        ticket = _create_approved_ticket(
            self.sa1, self.applicant, dm_role,
            ScopeType.DEPT, deptY.id, ticket_no='T-SYNC-2',
        )
        _apply_grant(ticket, actor=self.sa1)
        deptY.refresh_from_db()
        assert deptY.leader_id == self.applicant.id

    @pytest.mark.integration
    def test_grant_team_leader_keeps_existing_leader(self):
        """授予 team_leader 但团队已有组长 → 不覆盖原组长"""
        tl_role = Role.objects.get(role_key='team_leader')
        ticket = _create_approved_ticket(
            self.sa1, self.applicant, tl_role,
            ScopeType.TEAM, self.team1.id, ticket_no='T-SYNC-3',
        )
        _apply_grant(ticket, actor=self.sa1)
        self.team1.refresh_from_db()
        assert self.team1.leader_id == self.team_leader1.id


# ============================================================================
# 工具函数：change_summary 解析
# ============================================================================
class TestParseChangeSummary:
    """change_summary 解析兼容测试（脏数据不抛错）"""

    @pytest.mark.unit
    def test_empty_returns_none(self):
        """空值 → None"""
        assert parse_change_summary('') is None
        assert parse_change_summary(None) is None

    @pytest.mark.unit
    def test_valid_json_string_parsed(self):
        """合法 JSON 字符串 → 解析为 dict"""
        assert parse_change_summary('{"a": 1}') == {'a': 1}

    @pytest.mark.unit
    def test_invalid_json_returns_none(self):
        """非法 JSON 字符串 → None（脏数据不抛异常）"""
        assert parse_change_summary('{bad json') is None

    @pytest.mark.unit
    def test_object_passthrough(self):
        """已解析对象 → 原样返回"""
        assert parse_change_summary({'a': 1}) == {'a': 1}


# ============================================================================
# _can_approve_for_role 边界分支
# ============================================================================
class TestCanApproveForRoleEdge(TicketTestBase):
    """_can_approve_for_role 的异常/边界分支测试"""

    @pytest.mark.unit
    def test_none_user_returns_false(self):
        """user=None → False"""
        assert _can_approve_for_role(None, ApproverRole.SUPER_ADMIN) is False

    @pytest.mark.unit
    def test_unauthenticated_object_returns_false(self):
        """无 is_authenticated 属性的对象 → False"""
        assert _can_approve_for_role(object(), ApproverRole.SUPER_ADMIN) is False

    @pytest.mark.integration
    def test_unknown_role_returns_false(self):
        """未知 approver_role → False"""
        assert _can_approve_for_role(self.applicant, 'NO_SUCH_ROLE') is False

    @pytest.mark.integration
    def test_team_leader_no_current_node_returns_false(self):
        """TEAM_LEADER 但工单无当前节点 → False"""
        ticket = _make_pending_ticket(self.applicant, self.target_user, self.viewer, [])
        assert _can_approve_for_role(self.team_leader1, ApproverRole.TEAM_LEADER, ticket) is False

    @pytest.mark.integration
    def test_team_leader_no_scope_id_returns_false(self):
        """TEAM_LEADER 节点缺 approver_scope_id → False"""
        ticket = _make_pending_ticket(
            self.applicant, self.target_user, self.viewer,
            [{'approver_role': ApproverRole.TEAM_LEADER, 'approver_scope_id': None}])
        assert _can_approve_for_role(self.team_leader1, ApproverRole.TEAM_LEADER, ticket) is False

    @pytest.mark.integration
    def test_dept_leader_no_current_node_returns_false(self):
        """DEPT_LEADER 但工单无当前节点 → False"""
        ticket = _make_pending_ticket(self.applicant, self.target_user, self.viewer, [])
        assert _can_approve_for_role(self.dept_leader, ApproverRole.DEPT_LEADER, ticket) is False

    @pytest.mark.integration
    def test_dept_leader_no_scope_id_returns_false(self):
        """DEPT_LEADER 节点缺 approver_scope_id → False"""
        ticket = _make_pending_ticket(
            self.applicant, self.target_user, self.viewer,
            [{'approver_role': ApproverRole.DEPT_LEADER, 'approver_scope_id': None}])
        assert _can_approve_for_role(self.dept_leader, ApproverRole.DEPT_LEADER, ticket) is False

    @pytest.mark.integration
    def test_dept_leader_global_role_match(self):
        """DEPT_LEADER 走全局角色匹配（UserRoleRel + 所属部门）→ True"""
        node = _build_chain_node(ApproverRole.DEPT_LEADER, ScopeType.DEPT, self.dept.id)
        ticket = _make_pending_ticket(
            self.applicant, self.target_user, self.viewer,
            [node], ScopeType.DEPT, self.dept.id)
        # dept_leader 持有 dept_manager 全局角色且所属部门匹配
        assert _can_approve_for_role(self.dept_leader, ApproverRole.DEPT_LEADER, ticket) is True


# ============================================================================
# _find_approver_ids_for_role —— 共享审批池待办查询
# ============================================================================
class TestFindApproverIdsForRole(TicketTestBase):
    """_find_approver_ids_for_role 各角色池查询测试"""

    @pytest.mark.integration
    def test_super_admin_excludes_applicant(self):
        """超管池排除申请人（回避原则）"""
        ticket = _make_pending_ticket(
            self.sa1, self.target_user, Role.objects.get(role_key='super_admin'),
            [_build_chain_node(ApproverRole.SUPER_ADMIN)])
        ids = _find_approver_ids_for_role(ApproverRole.SUPER_ADMIN, ticket)
        assert self.sa1.id not in ids
        assert self.sa2.id in ids and self.sa3.id in ids

    @pytest.mark.integration
    def test_user_admin_finds(self):
        """user_admin 池返回所有持有者"""
        ids = _find_approver_ids_for_role(ApproverRole.USER_ADMIN)
        assert set(ids) == {self.user_admin1.id, self.user_admin2.id}

    @pytest.mark.integration
    def test_kb_admin_finds(self):
        """kb_admin 池返回所有持有者"""
        ids = _find_approver_ids_for_role(ApproverRole.KB_ADMIN)
        assert ids == [self.kb_admin1.id]

    @pytest.mark.integration
    def test_team_leader_no_current_node_returns_empty(self):
        """TEAM_LEADER 无当前节点 → 空列表"""
        ticket = _make_pending_ticket(self.applicant, self.target_user, self.viewer, [])
        assert _find_approver_ids_for_role(ApproverRole.TEAM_LEADER, ticket) == []

    @pytest.mark.integration
    def test_team_leader_team_missing_returns_empty(self):
        """TEAM_LEADER 目标团队不存在 → 空列表"""
        node = _build_chain_node(ApproverRole.TEAM_LEADER, ScopeType.TEAM, 999999)
        ticket = _make_pending_ticket(
            self.applicant, self.target_user, self.viewer, [node], ScopeType.TEAM, 999999)
        assert _find_approver_ids_for_role(ApproverRole.TEAM_LEADER, ticket) == []

    @pytest.mark.integration
    def test_team_leader_leader_excluded_returns_empty(self):
        """TEAM_LEADER 团队组长是申请人 → 被排除 → 空列表"""
        node = _build_chain_node(ApproverRole.TEAM_LEADER, ScopeType.TEAM, self.team1.id)
        ticket = _make_pending_ticket(
            self.team_leader1, self.target_user, self.viewer,
            [node], ScopeType.TEAM, self.team1.id)
        assert _find_approver_ids_for_role(ApproverRole.TEAM_LEADER, ticket) == []

    @pytest.mark.integration
    def test_team_leader_found(self):
        """TEAM_LEADER 团队组长可审 → 返回组长"""
        node = _build_chain_node(ApproverRole.TEAM_LEADER, ScopeType.TEAM, self.team1.id)
        ticket = _make_pending_ticket(
            self.applicant, self.target_user, self.viewer,
            [node], ScopeType.TEAM, self.team1.id)
        assert _find_approver_ids_for_role(ApproverRole.TEAM_LEADER, ticket) == [self.team_leader1.id]

    @pytest.mark.integration
    def test_dept_leader_no_current_node_returns_empty(self):
        """DEPT_LEADER 无当前节点 → 空列表"""
        ticket = _make_pending_ticket(self.applicant, self.target_user, self.viewer, [])
        assert _find_approver_ids_for_role(ApproverRole.DEPT_LEADER, ticket) == []

    @pytest.mark.integration
    def test_unknown_role_returns_empty(self):
        """未知角色 → 空列表"""
        assert _find_approver_ids_for_role('NO_SUCH_ROLE') == []


# ============================================================================
# 组长/经理解析降级
# ============================================================================
class TestLeaderResolverEdge(TicketTestBase):
    """_get_team_leader_id / _get_dept_leader_id / _get_super_admin_ids 边界测试"""

    @pytest.mark.integration
    def test_get_team_leader_id_none(self):
        """team_id 为空 → None"""
        assert _get_team_leader_id(None) is None

    @pytest.mark.integration
    def test_get_team_leader_id_found(self):
        """团队有组长 → 返回组长 ID"""
        assert _get_team_leader_id(self.team1.id) == self.team_leader1.id

    @pytest.mark.integration
    def test_get_team_leader_id_leader_missing(self):
        """团队无组长 → None"""
        teamX = Team.objects.create(name='无组长', code='nl', department=self.dept)
        assert _get_team_leader_id(teamX.id) is None

    @pytest.mark.integration
    def test_get_dept_leader_id_none(self):
        """dept_id 为空 → None"""
        assert _get_dept_leader_id(None) is None

    @pytest.mark.integration
    def test_get_dept_leader_id_from_scope_rel(self):
        """部门经理经 UserDeptScopeRel 授权 → 主源命中"""
        assert _get_dept_leader_id(self.dept2.id) == self.dept2_leader.id

    @pytest.mark.integration
    def test_get_super_admin_ids_no_matching_role(self):
        """无匹配角色 → 空列表"""
        assert _get_super_admin_ids(role_keys=('not_exist_role',)) == []


class TestResolveTeamLeader(TicketTestBase):
    """_resolve_team_leader 降级链测试（组长 → 部门经理 → 用户管理员 → 超管）"""

    @pytest.mark.integration
    def test_team_leader_matches(self):
        """团队组长可用 → 返回 TEAM_LEADER 节点"""
        assert _resolve_team_leader(self.team1.id) == \
            (ApproverRole.TEAM_LEADER, ScopeType.TEAM, self.team1.id)

    @pytest.mark.integration
    def test_team_leader_excluded_degrades_to_dept(self):
        """团队组长被排除（回避）→ 降级到本部门经理"""
        assert _resolve_team_leader(self.team1.id, exclude_user_id=self.team_leader1.id) == \
            (ApproverRole.DEPT_LEADER, ScopeType.DEPT, self.dept.id)

    @pytest.mark.integration
    def test_team_leader_no_dept_leader_degrades_to_user_admin(self):
        """团队组长缺失且部门无经理 → 降级到用户管理员"""
        deptX = Department.objects.create(name='空部门', code='empty_dept')
        teamX = Team.objects.create(name='空团队', code='et', department=deptX)
        assert _resolve_team_leader(teamX.id) == \
            (ApproverRole.USER_ADMIN, ScopeType.NONE, None)

    @pytest.mark.unit
    def test_no_team_and_no_user_admin_falls_back_super_admin(self):
        """无团队且无用户管理员 → 超管兜底"""
        with patch('apps.users.models.UserRoleRel.objects.filter') as mock_f:
            mock_f.return_value.exclude.return_value.exists.return_value = False
            assert _resolve_team_leader(None) == \
                (ApproverRole.SUPER_ADMIN, ScopeType.NONE, None)


# ============================================================================
# 审批链构造边界
# ============================================================================
class TestBuildApprovalChainEdge(TicketTestBase):
    """build_approval_chain 边界测试"""

    @pytest.mark.integration
    def test_role_none_returns_empty(self):
        """role=None → 空链"""
        assert build_approval_chain(
            self.applicant, self.target_user, TicketChangeType.GRANT,
            None, ScopeType.NONE, None) == []


class TestBuildGrantChainEdge(TicketTestBase):
    """_build_grant_chain_for_team_role 边界分支测试"""

    @pytest.mark.integration
    def test_viewer_dept_same_dept_empty(self):
        """部门级 viewer 本部门 → 空链（部门经理提单自动生效）"""
        chain = _build_grant_chain_for_team_role(self.applicant, 'viewer', ScopeType.DEPT, self.dept.id)
        assert chain == []

    @pytest.mark.integration
    def test_viewer_team_missing_team_falls_back_super(self):
        """团队级 viewer 目标团队不存在 → 超管兜底"""
        chain = _build_grant_chain_for_team_role(self.applicant, 'viewer', ScopeType.TEAM, 999999)
        assert [n['approver_role'] for n in chain] == [ApproverRole.SUPER_ADMIN]

    @pytest.mark.integration
    def test_viewer_team_same_team_empty(self):
        """团队级 viewer 本团队 → 空链"""
        chain = _build_grant_chain_for_team_role(self.applicant, 'viewer', ScopeType.TEAM, self.team1.id)
        assert chain == []

    @pytest.mark.integration
    def test_viewer_team_same_dept_other_team_empty(self):
        """团队级 viewer 本部门其他团队 → 空链"""
        chain = _build_grant_chain_for_team_role(self.applicant, 'viewer', ScopeType.TEAM, self.team2.id)
        assert chain == []

    @pytest.mark.integration
    def test_unknown_role_falls_back_super(self):
        """未知角色 → 超管单审"""
        chain = _build_grant_chain_for_team_role(self.applicant, 'unknown_role', ScopeType.NONE, None)
        assert [n['approver_role'] for n in chain] == [ApproverRole.SUPER_ADMIN]


class TestBuildRevokeChainEdge(TicketTestBase):
    """_build_revoke_chain_for_team_role team_leader 撤销分支测试"""

    @pytest.mark.integration
    def test_team_leader_wrong_scope_falls_back_super(self):
        """撤销 team_leader 但 scope 非 TEAM → 超管兜底"""
        chain = _build_revoke_chain_for_team_role(self.applicant, 'team_leader', ScopeType.NONE, None)
        assert [n['approver_role'] for n in chain] == [ApproverRole.SUPER_ADMIN]

    @pytest.mark.integration
    def test_team_leader_target_team_missing_falls_back_super(self):
        """撤销 team_leader 目标团队不存在 → 超管兜底"""
        chain = _build_revoke_chain_for_team_role(self.applicant, 'team_leader', ScopeType.TEAM, 999999)
        assert [n['approver_role'] for n in chain] == [ApproverRole.SUPER_ADMIN]

    @pytest.mark.integration
    def test_team_leader_same_dept_two_nodes(self):
        """撤销 team_leader 本部门 → 本部门经理 + 用户管理员 双审"""
        chain = _build_revoke_chain_for_team_role(self.applicant, 'team_leader', ScopeType.TEAM, self.team1.id)
        assert len(chain) == 2
        assert chain[0]['approver_role'] == ApproverRole.DEPT_LEADER
        assert chain[1]['approver_role'] == ApproverRole.USER_ADMIN

    @pytest.mark.integration
    def test_team_leader_cross_dept_two_nodes(self):
        """撤销 team_leader 跨部门 → 本部门经理 + 目标部门经理 双审"""
        chain = _build_revoke_chain_for_team_role(self.applicant, 'team_leader', ScopeType.TEAM, self.team3.id)
        assert len(chain) == 2
        assert chain[0]['approver_role'] == ApproverRole.DEPT_LEADER
        assert chain[1]['approver_role'] == ApproverRole.DEPT_LEADER

    @pytest.mark.integration
    def test_unknown_role_falls_back_super(self):
        """未知角色 → 超管单审"""
        chain = _build_revoke_chain_for_team_role(self.applicant, 'unknown_role', ScopeType.NONE, None)
        assert [n['approver_role'] for n in chain] == [ApproverRole.SUPER_ADMIN]


class TestSameTeamHelpers(TicketTestBase):
    """_is_same_team / _get_team_dept_id 小工具测试"""

    @pytest.mark.integration
    def test_is_same_team_no_target(self):
        """target_user 为空 → False"""
        assert _is_same_team(self.applicant, None, self.team1.id) is False

    @pytest.mark.integration
    def test_is_same_team_both_in_team(self):
        """两人同团队 → True"""
        other = _create_user('team8', team=self.team1, department=self.dept)
        assert _is_same_team(self.applicant, other, self.team1.id) is True

    @pytest.mark.integration
    def test_get_team_dept_id_none(self):
        """team_id 为空 → None"""
        assert _get_team_dept_id(None) is None

    @pytest.mark.integration
    def test_get_team_dept_id_found(self):
        """团队所属部门 → 返回部门 ID"""
        assert _get_team_dept_id(self.team1.id) == self.dept.id


# ============================================================================
# 审批/驳回异常分支
# ============================================================================
class TestApproveTicketEdge(TicketTestBase):
    """approve_ticket 异常分支测试"""

    @pytest.mark.integration
    def test_approve_chain_done_raises(self):
        """current_step 已超出审批链 → ValueError 审批链已完结"""
        dept3 = Department.objects.create(name='项目部2', code='proj2')
        ticket = create_ticket(
            self.sa1, self.target_user, TicketChangeType.GRANT,
            Role.objects.get(role_key='dept_manager'),
            ScopeType.DEPT, dept3.id)
        ticket.current_step = len(ticket.approval_chain)  # 模拟已完结
        ticket.save()
        with pytest.raises(ValueError, match='审批链已完结'):
            approve_ticket(ticket, self.user_admin1)

    @pytest.mark.integration
    def test_approve_node_locked_by_other_raises(self):
        """当前节点已被其他管理员锁定 → PermissionError"""
        dept3 = Department.objects.create(name='项目部3', code='proj3')
        ticket = create_ticket(
            self.sa1, self.target_user, TicketChangeType.GRANT,
            Role.objects.get(role_key='dept_manager'),
            ScopeType.DEPT, dept3.id)
        approve_ticket(ticket, self.user_admin1)  # 节点0 锁定为 ua1
        # 模拟并发：重新读取最新状态，回到节点0 再审批（节点已被 ua1 抢占）
        ticket.refresh_from_db()
        ticket.current_step = 0
        ticket.save()
        with pytest.raises(PermissionError, match='已被其他管理员处理'):
            approve_ticket(ticket, self.user_admin2)


class TestRejectTicketEdge(TicketTestBase):
    """reject_ticket 异常分支测试"""

    @pytest.mark.integration
    def test_reject_by_locked_approver_allowed(self):
        """当前节点审批人（已锁定）驳回 → 放行"""
        dept3 = Department.objects.create(name='项目部4', code='proj4')
        ticket = create_ticket(
            self.sa1, self.target_user, TicketChangeType.GRANT,
            Role.objects.get(role_key='dept_manager'),
            ScopeType.DEPT, dept3.id)
        chain = ticket.approval_chain
        chain[0]['approver_id'] = self.user_admin1.id  # 锁定当前节点给 ua1
        ticket.approval_chain = chain
        ticket.save()
        rejected = reject_ticket(ticket, self.user_admin1)
        assert rejected.status == TicketStatus.REJECTED

    @pytest.mark.integration
    def test_reject_wrong_role_raises(self):
        """非审批人且非超管驳回 → PermissionError"""
        ticket = create_ticket(
            self.applicant, self.target_user, TicketChangeType.GRANT,
            self.viewer, ScopeType.TEAM, self.team3.id)
        with pytest.raises(PermissionError, match='无权驳回'):
            reject_ticket(ticket, self.applicant)


# ============================================================================
# 执行分支：_apply_grant / _apply_revoke / _apply_extend / 角色变更
# ============================================================================
class TestApplyExecuteBranches(TicketTestBase):
    """授权执行各分支测试（GRANT 全局 / REVOKE 部门/团队 / EXPIRE_EXTEND / ROLE_CHANGE）"""

    def _make_approved(self, change_type, role, scope_type, scope_id, expires_at=None):
        """构造已 APPROVED 的指定变更类型工单（用于直接执行授权写入）"""
        import time as _t
        ticket = TicketList.objects.create(
            ticket_no=f'TA-{_t.time_ns() % 10**8}', title='执行测试',
            biz_type=TicketBizType.PERMISSION, applicant=self.sa1,
            status=TicketStatus.APPROVED, approval_chain=[], current_step=0,
        )
        TicketPermissionDetail.objects.create(
            ticket=ticket, target_user=self.target_user, change_type=change_type,
            role=role, scope_type=scope_type, scope_id=scope_id, reason='',
            expires_at=expires_at,
        )
        return ticket

    @pytest.mark.integration
    def test_apply_grant_global_role(self):
        """GRANT 全局角色 → UserRoleRel 写入 ACTIVE"""
        ticket = self._make_approved(
            TicketChangeType.GRANT, Role.objects.get(role_key='kb_admin'),
            ScopeType.NONE, None)
        _apply_grant(ticket, actor=self.sa1)
        assert UserRoleRel.objects.filter(
            user=self.target_user, role__role_key='kb_admin',
            status=GrantStatus.ACTIVE).exists()

    @pytest.mark.integration
    def test_apply_revoke_dept_scope(self):
        """REVOKE 部门级 → UserDeptScopeRel 置 REVOKED"""
        UserDeptScopeRel.objects.create(
            user=self.target_user, role=self.viewer, dept=self.dept,
            status=GrantStatus.ACTIVE)
        ticket = self._make_approved(
            TicketChangeType.REVOKE, self.viewer, ScopeType.DEPT, self.dept.id)
        _execute_grant_or_revoke(ticket, actor=self.sa1)
        rel = UserDeptScopeRel.objects.get(
            user=self.target_user, role=self.viewer, dept=self.dept)
        assert rel.status == GrantStatus.REVOKED

    @pytest.mark.integration
    def test_apply_revoke_team_scope(self):
        """REVOKE 团队级 → UserTeamScopeRel 置 REVOKED"""
        UserTeamScopeRel.objects.create(
            user=self.target_user, role=self.viewer, team=self.team2,
            status=GrantStatus.ACTIVE)
        ticket = self._make_approved(
            TicketChangeType.REVOKE, self.viewer, ScopeType.TEAM, self.team2.id)
        _execute_grant_or_revoke(ticket, actor=self.sa1)
        rel = UserTeamScopeRel.objects.get(
            user=self.target_user, role=self.viewer, team=self.team2)
        assert rel.status == GrantStatus.REVOKED

    @pytest.mark.integration
    def test_apply_extend_updates_expires(self):
        """EXPIRE_EXTEND → 更新授权记录 expires_at"""
        from django.utils import timezone as _tz
        new_expires = _tz.now() + _tz.timedelta(days=30)
        UserTeamScopeRel.objects.create(
            user=self.target_user, role=self.viewer, team=self.team2,
            status=GrantStatus.ACTIVE, expires_at=None)
        ticket = self._make_approved(
            TicketChangeType.EXPIRE_EXTEND, self.viewer,
            ScopeType.TEAM, self.team2.id, expires_at=new_expires)
        _execute_grant_or_revoke(ticket, actor=self.sa1)
        rel = UserTeamScopeRel.objects.get(
            user=self.target_user, role=self.viewer, team=self.team2)
        assert rel.expires_at is not None

    @pytest.mark.integration
    def test_apply_extend_noop_when_no_rels(self):
        """EXPIRE_EXTEND 无匹配授权 → 审计 result=NOOP"""
        ticket = self._make_approved(
            TicketChangeType.EXPIRE_EXTEND, self.viewer,
            ScopeType.TEAM, self.team2.id, expires_at=None)
        _execute_grant_or_revoke(ticket, actor=self.sa1)
        assert PermissionAuditLog.objects.filter(
            action='EXPIRE_EXTEND', target_id=ticket.id, result='NOOP').exists()

    @pytest.mark.integration
    def test_role_change_dept_scope_revokes_old(self):
        """ROLE_CHANGE 部门级 → 撤销旧角色 + 授予新角色（原子）"""
        deptX = Department.objects.create(name='变更部', code='chg_dept')
        UserDeptScopeRel.objects.create(
            user=self.target_user, role=self.contributor, dept=deptX,
            status=GrantStatus.ACTIVE)
        ticket = _make_pending_ticket(
            self.sa1, self.target_user, self.viewer, [],
            ScopeType.DEPT, deptX.id,
            change_type=TicketChangeType.ROLE_CHANGE, ticket_no='TA-RC-1')
        pd = ticket.permission_detail
        pd.previous_role = self.contributor
        pd.save()
        _execute_grant_or_revoke(ticket, actor=self.sa1)
        assert UserDeptScopeRel.objects.filter(
            user=self.target_user, role=self.contributor, dept=deptX,
            status=GrantStatus.REVOKED).exists()
        assert UserDeptScopeRel.objects.filter(
            user=self.target_user, role=self.viewer, dept=deptX,
            status=GrantStatus.ACTIVE).exists()

    @pytest.mark.integration
    def test_sync_leader_dept_manager_revoke(self):
        """撤销 dept_manager → Department.leader 清空"""
        deptZ = Department.objects.create(name='撤经理部', code='rm_mgr')
        deptZ.leader = self.target_user
        deptZ.save()
        ticket = self._make_approved(
            TicketChangeType.REVOKE, Role.objects.get(role_key='dept_manager'),
            ScopeType.DEPT, deptZ.id)
        _sync_leader_for_role(ticket, Role.objects.get(role_key='dept_manager'), grant=False)
        deptZ.refresh_from_db()
        assert deptZ.leader_id is None


# ============================================================================
# SoD 互斥 + 团队/部门角色互斥检测
# ============================================================================
class TestSodAndDetectEdges(TicketTestBase):
    """_check_sod_conflict / _detect_team_role_in_service / _detect_dept_role_in_service 边界测试"""

    @pytest.mark.integration
    def test_sod_conflict_no_role_returns(self):
        """new_role=None → 直接返回不报错"""
        _check_sod_conflict(self.target_user, None)  # 不应抛错

    @pytest.mark.integration
    def test_sod_conflict_duplicate_role_allowed(self):
        """用户已持有申请角色 → 非冲突（重复授权由调用方判定）"""
        _grant_role(self.target_user, 'kb_admin')
        _check_sod_conflict(self.target_user, Role.objects.get(role_key='kb_admin'))

    @pytest.mark.integration
    def test_sod_conflict_raises_on_conflict(self):
        """用户已持有互斥角色 → 抛 ValueError"""
        _grant_role(self.target_user, 'kb_admin')
        RoleConflictRule.objects.get_or_create(
            role_a=Role.objects.get(role_key='user_admin'),
            role_b=Role.objects.get(role_key='kb_admin'),
            defaults={'reason': '4 高权 4 选 1'})
        with pytest.raises(ValueError, match='SoD 互斥冲突'):
            _check_sod_conflict(self.target_user, Role.objects.get(role_key='user_admin'))

    @pytest.mark.integration
    def test_detect_team_role_none_args(self):
        """user/team_id 为空 → None"""
        assert _detect_team_role_in_service(None, self.team1.id) is None
        assert _detect_team_role_in_service(self.target_user, None) is None

    @pytest.mark.integration
    def test_detect_dept_role_none_args(self):
        """user/dept_id 为空 → None"""
        assert _detect_dept_role_in_service(None, self.dept.id) is None
        assert _detect_dept_role_in_service(self.target_user, None) is None

    @pytest.mark.integration
    def test_detect_dept_role_found(self):
        """用户已持有部门级团队角色 → 返回角色对象"""
        UserDeptScopeRel.objects.create(
            user=self.target_user, role=self.viewer, dept=self.dept,
            status=GrantStatus.ACTIVE)
        role = _detect_dept_role_in_service(self.target_user, self.dept.id)
        assert role is not None and role.role_key == 'viewer'


# ============================================================================
# 待办候选池_find_approver_ids_for_role
# ============================================================================
class TestFindApproverIdsEdge(TicketTestBase):
    """_find_approver_ids_for_role：TEAM_LEADER 无 scope / DEPT_LEADER 全部分支"""

    @pytest.mark.integration
    def test_team_leader_node_without_scope_id_returns_empty(self):
        """TEAM_LEADER 节点无 approver_scope_id → 空候选池（共享审批池无法匹配团队）"""
        ticket = _make_pending_ticket(
            self.applicant, self.target_user, self.viewer,
            [_build_chain_node(ApproverRole.TEAM_LEADER)],
            ScopeType.TEAM, self.team1.id, ticket_no='TA-TL-NO')
        assert _find_approver_ids_for_role(ApproverRole.TEAM_LEADER, ticket) == []

    @pytest.mark.integration
    def test_dept_leader_node_without_scope_id_returns_empty(self):
        """DEPT_LEADER 节点无 approver_scope_id → 空候选池"""
        ticket = _make_pending_ticket(
            self.applicant, self.target_user, self.viewer,
            [_build_chain_node(ApproverRole.DEPT_LEADER)],
            ScopeType.TEAM, self.team1.id, ticket_no='TA-DL-NO')
        assert _find_approver_ids_for_role(ApproverRole.DEPT_LEADER, ticket) == []

    @pytest.mark.integration
    def test_dept_leader_scope_rel_found(self):
        """DEPT_LEADER 命中 UserDeptScopeRel 属地授权 → 返回该部门经理"""
        ticket = _make_pending_ticket(
            self.applicant, self.target_user, self.viewer,
            [_build_chain_node(ApproverRole.DEPT_LEADER, ScopeType.DEPT, self.dept2.id)],
            ScopeType.TEAM, self.team1.id, ticket_no='TA-DL-REL')
        assert _find_approver_ids_for_role(ApproverRole.DEPT_LEADER, ticket) == [self.dept2_leader.id]

    @pytest.mark.integration
    def test_dept_leader_global_role_fallback(self):
        """DEPT_LEADER 无属地授权 → 全局 dept_manager + 用户所属部门匹配兜底"""
        ticket = _make_pending_ticket(
            self.applicant, self.target_user, self.viewer,
            [_build_chain_node(ApproverRole.DEPT_LEADER, ScopeType.DEPT, self.dept.id)],
            ScopeType.TEAM, self.team1.id, ticket_no='TA-DL-GL')
        assert _find_approver_ids_for_role(ApproverRole.DEPT_LEADER, ticket) == [self.dept_leader.id]

    @pytest.mark.integration
    def test_dept_leader_no_manager_returns_empty(self):
        """DEPT_LEADER 部门无任何 dept_manager 授权 → 空候选池"""
        deptX = Department.objects.create(name='无经理部', code='no_mgr')
        ticket = _make_pending_ticket(
            self.applicant, self.target_user, self.viewer,
            [_build_chain_node(ApproverRole.DEPT_LEADER, ScopeType.DEPT, deptX.id)],
            ScopeType.TEAM, self.team1.id, ticket_no='TA-DL-EM')
        assert _find_approver_ids_for_role(ApproverRole.DEPT_LEADER, ticket) == []


# ============================================================================
# 部门经理降级链_resolve_dept_leader
# ============================================================================
class TestResolveDeptLeaderEdge(TicketTestBase):
    """_resolve_dept_leader：部门经理缺失时降级到用户管理员 / 超管"""

    @pytest.mark.integration
    def test_dept_without_manager_degrades_to_user_admin(self):
        """部门无经理授权且存在 user_admin → USER_ADMIN 兜底"""
        deptX = Department.objects.create(name='无经理降级部', code='dgl_1')
        assert _resolve_dept_leader(deptX.id) == (ApproverRole.USER_ADMIN, ScopeType.NONE, None)

    @pytest.mark.integration
    def test_dept_without_manager_no_user_admin_degrades_super(self):
        """部门无经理授权且无 user_admin → SUPER_ADMIN 兜底"""
        deptX = Department.objects.create(name='无经理超管部', code='dgl_2')
        UserRoleRel.objects.filter(role__role_key='user_admin').delete()
        assert _resolve_dept_leader(deptX.id) == (ApproverRole.SUPER_ADMIN, ScopeType.NONE, None)

    @pytest.mark.integration
    def test_none_dept_with_user_admin_degrades_to_user_admin(self):
        """dept_id 为空且存在 user_admin → USER_ADMIN 兜底"""
        assert _resolve_dept_leader(None) == (ApproverRole.USER_ADMIN, ScopeType.NONE, None)

    @pytest.mark.integration
    def test_none_dept_no_user_admin_degrades_super(self):
        """dept_id 为空且无 user_admin → SUPER_ADMIN 兜底"""
        UserRoleRel.objects.filter(role__role_key='user_admin').delete()
        assert _resolve_dept_leader(None) == (ApproverRole.SUPER_ADMIN, ScopeType.NONE, None)


# ============================================================================
# 授权申请链兜底分支_build_grant_chain_for_team_role
# ============================================================================
class TestBuildGrantChainMore(TicketTestBase):
    """_build_grant_chain_for_team_role：部门级无 scope_id / 非团队 scope / team_leader 无 user_admin"""

    @pytest.mark.integration
    def test_viewer_dept_scope_missing_dept_id_falls_back_super(self):
        """viewer 部门级但 scope_id 为空 → SUPER_ADMIN 兜底"""
        chain = _build_grant_chain_for_team_role(self.applicant, 'viewer', ScopeType.DEPT, None)
        assert chain[0]['approver_role'] == ApproverRole.SUPER_ADMIN

    @pytest.mark.integration
    def test_viewer_global_scope_falls_back_super(self):
        """viewer 非部门/团队 scope（如全局） → SUPER_ADMIN 兜底"""
        chain = _build_grant_chain_for_team_role(self.applicant, 'viewer', ScopeType.NONE, None)
        assert chain[0]['approver_role'] == ApproverRole.SUPER_ADMIN

    @pytest.mark.integration
    def test_team_leader_without_user_admin_falls_back_super(self):
        """team_leader 无可用 user_admin → SUPER_ADMIN 兜底"""
        with patch('apps.users.ticket_service.UserRoleRel.objects.filter') as mf:
            mf.return_value.exclude.return_value.exists.return_value = False
            chain = _build_grant_chain_for_team_role(
                self.applicant, 'team_leader', ScopeType.TEAM, self.team1.id)
        assert chain[0]['approver_role'] == ApproverRole.SUPER_ADMIN


# ============================================================================
# 工单号生成_gen_ticket_no 当日全局序列
# ============================================================================
class TestGenTicketNo(TicketTestBase):
    """_gen_ticket_no：新格式工单当日序列递增 / 跨日期不递增"""

    @pytest.mark.integration
    def test_same_day_new_format_increments_seq(self):
        """存在当日新格式工单 → 序列号 +1（当日全局序列）"""
        from django.utils import timezone as _tz
        today = _tz.localtime().strftime('%Y%m%d')
        _make_pending_ticket(self.sa1, self.target_user, self.viewer, [],
                             ticket_no=f'QX{today}0001')
        assert _gen_ticket_no(TicketBizType.PERMISSION) == f'QX{today}0002'

    @pytest.mark.integration
    def test_old_date_new_format_keeps_seq_one(self):
        """仅存在历史日期新格式工单 → 序列不递增（当日序列从 1 开始）"""
        from django.utils import timezone as _tz
        today = _tz.localtime().strftime('%Y%m%d')
        _make_pending_ticket(self.sa1, self.target_user, self.viewer, [],
                             ticket_no='QX202601010001')
        assert _gen_ticket_no(TicketBizType.PERMISSION) == f'QX{today}0001'


# ============================================================================
# leader 同步剩余分支_sync_leader_for_role
# ============================================================================
class TestSyncLeaderRemaining(TicketTestBase):
    """_sync_leader_for_role：role 为空直接返回 / 撤销团队组长清空 leader"""

    @pytest.mark.integration
    def test_role_none_returns_immediately(self):
        """role=None → 直接返回不处理（避免空角色同步）"""
        ticket = _make_pending_ticket(
            self.sa1, self.target_user, self.viewer, [],
            ScopeType.TEAM, self.team1.id, ticket_no='TA-SL-NR')
        _sync_leader_for_role(ticket, None, grant=True)  # 不应抛错

    @pytest.mark.integration
    def test_revoke_team_leader_clears_team_leader(self):
        """撤销 team_leader → 现任组长对应 Team.leader 清空"""
        team_leader_role = Role.objects.get(role_key='team_leader')
        ticket = _make_pending_ticket(
            self.sa1, self.team_leader1, team_leader_role, [],
            ScopeType.TEAM, self.team1.id,
            change_type=TicketChangeType.REVOKE, ticket_no='TA-SL-RV')
        _sync_leader_for_role(ticket, team_leader_role, grant=False)
        self.team1.refresh_from_db()
        assert self.team1.leader_id is None


# ============================================================================
# 角色变更 / 撤销的全局角色分支_apply_role_change / _apply_revoke
# ============================================================================
class TestRoleChangeAndRevokeGlobal(TicketTestBase):
    """ROLE_CHANGE / REVOKE 全局角色时 UserRoleRel 被 update 命中的分支"""

    @pytest.mark.integration
    def test_role_change_global_revokes_old_via_user_role_rel(self):
        """ROLE_CHANGE 全局旧角色（kb_admin）经 UserRoleRel 撤销 → 新角色 viewer 生效"""
        _grant_role(self.target_user, 'kb_admin')
        ticket = _make_pending_ticket(
            self.sa1, self.target_user, self.viewer, [],
            ScopeType.NONE, None,
            change_type=TicketChangeType.ROLE_CHANGE, ticket_no='TA-RC-GL')
        ticket.permission_detail.previous_role = Role.objects.get(role_key='kb_admin')
        ticket.permission_detail.save()
        _apply_role_change(ticket, actor=self.sa1)
        assert UserRoleRel.objects.filter(
            user=self.target_user, role__role_key='kb_admin',
            status=GrantStatus.REVOKED).exists()
        assert UserRoleRel.objects.filter(
            user=self.target_user, role__role_key='viewer',
            status=GrantStatus.ACTIVE).exists()

    @pytest.mark.integration
    def test_apply_revoke_global_role_hits_user_role_rel(self):
        """REVOKE 全局角色 → UserRoleRel 被 update 置 REVOKED"""
        _grant_role(self.target_user, 'kb_admin')
        ticket = _create_approved_ticket(
            self.sa1, self.target_user, Role.objects.get(role_key='kb_admin'),
            ScopeType.NONE, None, 'TA-RV-GL')
        _apply_revoke(ticket, actor=self.sa1)
        assert UserRoleRel.objects.filter(
            user=self.target_user, role__role_key='kb_admin',
            status=GrantStatus.REVOKED).exists()
