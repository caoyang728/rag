"""
apps.users.services.ticket_role 单元/集成测试 —— 角色配置变更工单服务

覆盖范围（针对 83% 覆盖率的缺口）：
- _get_role_risk_level：全操作 + 未知操作默认值
- _build_role_approval_chain：normal / high 审批链构造
- _check_role_approver_quota：超管不足抛 ValueError
- create_role_ticket：ADD / EDIT / DELETE / ASSIGN_PERMS 全操作类型
- _execute_role_change：缺少 detail / 未知操作类型
- _write_role_audit：审计写入异常不阻断主业务
- _apply_role_add：空 code/name 校验、IntegrityError 恢复冲突、正常新增、软删恢复
- _apply_role_edit：目标角色不存在、内置角色改码、编码重复、正常编辑
- _apply_role_delete：目标不存在(幂等)、已软删(幂等)、内置角色、有用户绑定、正常删除
- _apply_role_assign_perms：目标角色不存在、正常分配

采用 DB 集成测试（@pytest.mark.integration）+ unittest.mock 混合：
- 工单创建/角色操作走真实 DB（验证状态机、唯一性约束、事务回滚）
- 审计写入 / 创建重试等外部依赖用 mock 隔离
"""
import pytest
from unittest.mock import patch, MagicMock

from apps.users.models import (
    User, Role, Department, Team, UserRoleRel,
    TicketList, TicketRoleDetail, PermissionAuditLog,
    TicketStatus, TicketBizType, RoleOperation,
    RoleType, DataScope, AuditTargetType, GrantStatus,
)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _create_user(username, **extra):
    """创建测试用户"""
    return User.objects.create_user(
        username=username, email=f'{username}@test.com',
        password='pass12345', **extra)


def _create_role(role_key, name=None, **defaults):
    """创建角色，自动补齐默认字段"""
    return Role.objects.create(
        role_key=role_key,
        name=name or role_key,
        role_type=defaults.pop('role_type', RoleType.NORMAL_USER),
        data_scope=defaults.pop('data_scope', DataScope.TEAM),
        is_builtin=defaults.pop('is_builtin', False),
        **defaults,
    )


def _make_role_ticket(actor, operation, target_role=None,
                      old_data=None, new_data=None,
                      permission_ids=None, status=TicketStatus.PENDING):
    """构造角色工单（主表 + 详情子表），跳过审批链直接创建，供执行层测试用"""
    ticket = TicketList.objects.create(
        ticket_no=f'ROLE-TEST-{operation}-{target_role.id if target_role else "NEW"}',
        title=f'测试工单: {operation}',
        biz_type=TicketBizType.ROLE,
        status=status,
        risk_level='normal',
        applicant=actor,
        approval_chain=[],
        current_step=0,
        operation=operation,
    )
    TicketRoleDetail.objects.create(
        ticket=ticket,
        operation=operation,
        target_role=target_role,
        old_data=old_data,
        new_data=new_data,
        permission_ids=permission_ids or [],
        reason='测试',
    )
    return ticket


# ---------------------------------------------------------------------------
# 1. _get_role_risk_level 测试
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGetRoleRiskLevel:
    """_get_role_risk_level 全操作 + 未知操作返回默认值"""

    def test_when_add_then_returns_normal(self):
        from apps.users.services.ticket_role import _get_role_risk_level
        assert _get_role_risk_level(RoleOperation.ADD) == 'normal'

    def test_when_edit_then_returns_normal(self):
        from apps.users.services.ticket_role import _get_role_risk_level
        assert _get_role_risk_level(RoleOperation.EDIT) == 'normal'

    def test_when_assign_perms_then_returns_normal(self):
        from apps.users.services.ticket_role import _get_role_risk_level
        assert _get_role_risk_level(RoleOperation.ASSIGN_PERMS) == 'normal'

    def test_when_delete_then_returns_high(self):
        from apps.users.services.ticket_role import _get_role_risk_level
        assert _get_role_risk_level(RoleOperation.DELETE) == 'high'

    def test_when_unknown_operation_then_returns_normal(self):
        from apps.users.services.ticket_role import _get_role_risk_level
        assert _get_role_risk_level('unknown_op') == 'normal'


# ---------------------------------------------------------------------------
# 2. _build_role_approval_chain 测试
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBuildRoleApprovalChain:
    """_build_role_approval_chain normal / high 两条分支"""

    def test_when_normal_then_single_admin_chain(self):
        from apps.users.services.ticket_role import _build_role_approval_chain
        chain = _build_role_approval_chain('normal')
        # normal 应返回单节点审批链
        assert len(chain) == 1

    def test_when_high_then_dual_admin_chain(self):
        from apps.users.services.ticket_role import _build_role_approval_chain
        chain = _build_role_approval_chain('high')
        # high 应返回双超管复核链（2步）
        assert len(chain) == 2


# ---------------------------------------------------------------------------
# 3. _check_role_approver_quota 测试
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCheckRoleApproverQuota:
    """_check_role_approver_quota 超管不足时抛 ValueError"""

    def test_when_insufficient_single_then_raises(self):
        """单审场景：排除申请人后无可用超管，应抛 ValueError"""
        from apps.users.services.ticket_role import _check_role_approver_quota
        applicant = _create_user('quota applicant')
        with patch('apps.users.services.ticket_role._get_super_admin_ids', return_value=[]):
            with pytest.raises(ValueError, match='可用超级管理员不足'):
                _check_role_approver_quota(applicant, 'normal')

    def test_when_insufficient_dual_then_raises(self):
        """双审场景：只有 1 个可用超管，不足 2 人，应抛 ValueError"""
        from apps.users.services.ticket_role import _check_role_approver_quota
        applicant = _create_user('quota applicant dual')
        with patch('apps.users.services.ticket_role._get_super_admin_ids', return_value=[1]):
            with pytest.raises(ValueError, match='可用超级管理员不足'):
                _check_role_approver_quota(applicant, 'high')

    def test_when_sufficient_then_no_raise(self):
        """单审场景：有 1 个可用超管，不应抛异常"""
        from apps.users.services.ticket_role import _check_role_approver_quota
        applicant = _create_user('quota applicant ok')
        with patch('apps.users.services.ticket_role._get_super_admin_ids', return_value=[1]):
            _check_role_approver_quota(applicant, 'normal')  # 无异常

    def test_when_sufficient_dual_then_no_raise(self):
        """双审场景：有 2 个可用超管，不应抛异常"""
        from apps.users.services.ticket_role import _check_role_approver_quota
        applicant = _create_user('quota applicant dual ok')
        with patch('apps.users.services.ticket_role._get_super_admin_ids', return_value=[1, 2]):
            _check_role_approver_quota(applicant, 'high')  # 无异常


# ---------------------------------------------------------------------------
# 4. create_role_ticket 测试
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCreateRoleTicket:
    """create_role_ticket 各操作类型创建工单"""

    def test_when_add_operation_then_creates_ticket(self):
        """ADD 操作创建工单：biz_type=ROLE, status=PENDING, risk_level=normal"""
        from apps.users.services.ticket_role import create_role_ticket
        actor = _create_user('ticket add actor')
        with patch('apps.users.services.ticket_role._get_super_admin_ids', return_value=[99]):
            ticket = create_role_ticket(
                actor=actor,
                operation=RoleOperation.ADD,
                new_data={'code': 'test_add', 'name': '测试新增角色'},
                reason='测试新增',
            )
        assert ticket.biz_type == TicketBizType.ROLE
        assert ticket.status == TicketStatus.PENDING
        assert ticket.risk_level == 'normal'
        assert ticket.operation == RoleOperation.ADD
        # 验证详情子表
        detail = ticket.role_detail
        assert detail.operation == RoleOperation.ADD
        assert detail.new_data['code'] == 'test_add'

    def test_when_edit_operation_then_creates_ticket(self):
        """EDIT 操作创建工单"""
        from apps.users.services.ticket_role import create_role_ticket
        actor = _create_user('ticket edit actor')
        role = _create_role('edit_role', name='编辑角色')
        with patch('apps.users.services.ticket_role._get_super_admin_ids', return_value=[99]):
            ticket = create_role_ticket(
                actor=actor,
                operation=RoleOperation.EDIT,
                target_role=role,
                old_data={'name': '编辑角色'},
                new_data={'name': '新名字'},
                reason='测试编辑',
            )
        assert ticket.operation == RoleOperation.EDIT
        assert ticket.role_detail.target_role == role

    def test_when_delete_operation_then_risk_is_high(self):
        """DELETE 操作创建工单：risk_level=high"""
        from apps.users.services.ticket_role import create_role_ticket
        actor = _create_user('ticket del actor')
        role = _create_role('del_role', name='删除角色')
        with patch('apps.users.services.ticket_role._get_super_admin_ids', return_value=[99, 98]):
            ticket = create_role_ticket(
                actor=actor,
                operation=RoleOperation.DELETE,
                target_role=role,
                old_data={'name': '删除角色'},
                reason='测试删除',
            )
        assert ticket.risk_level == 'high'
        assert ticket.operation == RoleOperation.DELETE

    def test_when_assign_perms_then_creates_ticket(self):
        """ASSIGN_PERMS 操作创建工单"""
        from apps.users.services.ticket_role import create_role_ticket
        actor = _create_user('ticket assign actor')
        role = _create_role('assign_role', name='权限分配角色')
        with patch('apps.users.services.ticket_role._get_super_admin_ids', return_value=[99]):
            ticket = create_role_ticket(
                actor=actor,
                operation=RoleOperation.ASSIGN_PERMS,
                target_role=role,
                permission_ids=[1, 2, 3],
                reason='测试权限分配',
            )
        assert ticket.operation == RoleOperation.ASSIGN_PERMS
        assert ticket.role_detail.permission_ids == [1, 2, 3]


# ---------------------------------------------------------------------------
# 5. _execute_role_change 测试
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestExecuteRoleChange:
    """_execute_role_change 缺少 detail / 未知操作类型"""

    def test_when_detail_missing_then_raises(self):
        """工单缺少 role_detail 时抛 ValueError"""
        from apps.users.services.ticket_role import _execute_role_change
        actor = _create_user('exec actor')
        # 构造一个没有 TicketRoleDetail 的工单
        ticket = TicketList.objects.create(
            ticket_no='EXEC-NO-DETAIL',
            title='无详情工单',
            biz_type=TicketBizType.ROLE,
            status=TicketStatus.APPROVED,
            risk_level='normal',
            applicant=actor,
            approval_chain=[],
            current_step=0,
            operation=RoleOperation.ADD,
        )
        with pytest.raises(ValueError, match='缺少 role_detail'):
            _execute_role_change(ticket, actor)

    def test_when_unknown_operation_then_raises(self):
        """未知操作类型抛 ValueError"""
        from apps.users.services.ticket_role import _execute_role_change
        actor = _create_user('exec unknown actor')
        ticket = _make_role_ticket(actor, 'unknown_op')
        # 将 operation 改为未知值
        ticket.operation = 'unknown_op'
        ticket.save(update_fields=['operation'])
        ticket.role_detail.operation = 'unknown_op'
        ticket.role_detail.save(update_fields=['operation'])
        with pytest.raises(ValueError, match='未知的角色操作类型'):
            _execute_role_change(ticket, actor)


# ---------------------------------------------------------------------------
# 6. _write_role_audit 异常处理测试
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestWriteRoleAudit:
    """_write_role_audit 写入异常不阻断主业务"""

    @patch('apps.users.services.ticket_role.PermissionAuditLog.objects')
    def test_when_db_error_then_no_raise(self, mock_objects):
        """PermissionAuditLog 写入异常仅记日志，不向上抛"""
        from apps.users.services.ticket_role import _write_role_audit
        actor = _create_user('audit actor')
        role = _create_role('audit_role', name='审计测试角色')
        ticket = _make_role_ticket(actor, RoleOperation.ADD, target_role=role)
        mock_objects.create.side_effect = Exception('DB write error')
        # 不应抛异常
        _write_role_audit(ticket, actor, 'ROLE_CREATE',
                          before=None, after={'id': role.id})
        mock_objects.create.assert_called_once()


# ---------------------------------------------------------------------------
# 7. _apply_role_add 测试
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestApplyRoleAdd:
    """_apply_role_add 各分支：空字段、冲突、正常、恢复"""

    def test_when_code_empty_then_raises(self):
        """code 为空时抛 ValueError"""
        from apps.users.services.ticket_role import _apply_role_add
        actor = _create_user('add empty code actor')
        ticket = _make_role_ticket(
            actor, RoleOperation.ADD,
            new_data={'code': '', 'name': '有名字'},
        )
        with pytest.raises(ValueError, match='角色编码与名称不能为空'):
            _apply_role_add(ticket, actor)

    def test_when_name_empty_then_raises(self):
        """name 为空时抛 ValueError"""
        from apps.users.services.ticket_role import _apply_role_add
        actor = _create_user('add empty name actor')
        ticket = _make_role_ticket(
            actor, RoleOperation.ADD,
            new_data={'code': 'valid_code', 'name': ''},
        )
        with pytest.raises(ValueError, match='角色编码与名称不能为空'):
            _apply_role_add(ticket, actor)

    def test_when_code_exists_active_then_raises(self):
        """同名 active 角色已存在时抛 ValueError"""
        from apps.users.services.ticket_role import _apply_role_add
        actor = _create_user('add dup actor')
        _create_role('dup_code', name='已存在角色')
        ticket = _make_role_ticket(
            actor, RoleOperation.ADD,
            new_data={'code': 'dup_code', 'name': '同名新增'},
        )
        with pytest.raises(ValueError, match='角色编码已存在'):
            _apply_role_add(ticket, actor)

    def test_when_normal_add_then_creates_role(self):
        """正常新增角色，验证 Role 记录创建"""
        from apps.users.services.ticket_role import _apply_role_add
        actor = _create_user('add normal actor')
        ticket = _make_role_ticket(
            actor, RoleOperation.ADD,
            new_data={
                'code': 'new_role_normal',
                'name': '正常新增角色',
                'description': '测试描述',
                'role_type': RoleType.NORMAL_USER,
                'data_scope': DataScope.TEAM,
            },
        )
        _apply_role_add(ticket, actor)
        role = Role.objects.get(role_key='new_role_normal')
        assert role.name == '正常新增角色'
        assert role.description == '测试描述'
        assert role.is_builtin is False
        assert role.is_deleted is False

    def test_when_restore_deleted_role_then_unsoft_delete(self):
        """软删角色恢复：同名 is_deleted=True 角色被恢复而非新建"""
        from apps.users.services.ticket_role import _apply_role_add
        actor = _create_user('add restore actor')
        # 先创建再软删
        deleted_role = _create_role('restorable_role', name='被删角色')
        deleted_role.is_deleted = True
        deleted_role.save(update_fields=['is_deleted'])
        ticket = _make_role_ticket(
            actor, RoleOperation.ADD,
            new_data={'code': 'restorable_role', 'name': '恢复角色'},
        )
        _apply_role_add(ticket, actor)
        deleted_role.refresh_from_db()
        assert deleted_role.is_deleted is False
        assert deleted_role.name == '恢复角色'

    def test_when_restore_integrity_error_then_raises(self):
        """恢复软删角色时 IntegrityError → 抛 ValueError（模拟并发竞态）"""
        from apps.users.services.ticket_role import _apply_role_add
        from django.db import IntegrityError
        actor = _create_user('add restore conflict actor')
        import uuid
        unique_key = f'rc_{uuid.uuid4().hex[:8]}'
        # 创建一个软删角色
        deleted_role = _create_role(unique_key, name='冲突角色')
        deleted_role.is_deleted = True
        deleted_role.save(update_fields=['is_deleted'])
        ticket = _make_role_ticket(
            actor, RoleOperation.ADD,
            new_data={'code': unique_key, 'name': '恢复角色'},
        )
        # mock Role.save 在恢复时抛 IntegrityError（模拟并发竞态）
        with patch('apps.users.models.Role.save', side_effect=IntegrityError('duplicate key')):
            with pytest.raises(ValueError, match='角色编码已存在'):
                _apply_role_add(ticket, actor)


# ---------------------------------------------------------------------------
# 8. _apply_role_edit 测试
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestApplyRoleEdit:
    """_apply_role_edit 各分支：目标缺失、内置改码、编码重复、正常编辑"""

    def test_when_target_role_none_then_raises(self):
        """target_role 为 None 时抛 ValueError"""
        from apps.users.services.ticket_role import _apply_role_edit
        actor = _create_user('edit none actor')
        ticket = _make_role_ticket(
            actor, RoleOperation.EDIT,
            target_role=None,
            new_data={'name': '新名字'},
        )
        with pytest.raises(ValueError, match='目标角色不存在'):
            _apply_role_edit(ticket, actor)

    def test_when_builtin_role_code_change_then_raises(self):
        """内置角色编码修改抛 ValueError"""
        from apps.users.services.ticket_role import _apply_role_edit
        actor = _create_user('edit builtin actor')
        builtin_role = _create_role('super_admin', name='超管', is_builtin=True)
        ticket = _make_role_ticket(
            actor, RoleOperation.EDIT,
            target_role=builtin_role,
            old_data={'name': '超管'},
            new_data={'name': '超管', 'code': 'new_super_admin'},
        )
        with pytest.raises(ValueError, match='内置角色编码不可修改'):
            _apply_role_edit(ticket, actor)

    def test_when_duplicate_code_then_raises(self):
        """编码修改时编码重复抛 ValueError"""
        from apps.users.services.ticket_role import _apply_role_edit
        actor = _create_user('edit dup actor')
        role = _create_role('editable_role', name='可编辑角色')
        _create_role('taken_code', name='已占用编码')
        ticket = _make_role_ticket(
            actor, RoleOperation.EDIT,
            target_role=role,
            old_data={'name': '可编辑角色'},
            new_data={'name': '新名字', 'code': 'taken_code'},
        )
        with pytest.raises(ValueError, match='角色编码已存在'):
            _apply_role_edit(ticket, actor)

    def test_when_normal_edit_then_updates_role(self):
        """正常编辑：修改名称和描述"""
        from apps.users.services.ticket_role import _apply_role_edit
        actor = _create_user('edit normal actor')
        role = _create_role('normal_edit_role', name='原名', description='原描述')
        ticket = _make_role_ticket(
            actor, RoleOperation.EDIT,
            target_role=role,
            old_data={'name': '原名', 'description': '原描述'},
            new_data={'name': '新名称', 'description': '新描述'},
        )
        _apply_role_edit(ticket, actor)
        role.refresh_from_db()
        assert role.name == '新名称'
        assert role.description == '新描述'

    def test_when_edit_code_only_then_updates_role_key(self):
        """仅修改编码（非内置角色），验证 role_key 更新"""
        from apps.users.services.ticket_role import _apply_role_edit
        actor = _create_user('edit code actor')
        role = _create_role('old_code', name='编码角色')
        ticket = _make_role_ticket(
            actor, RoleOperation.EDIT,
            target_role=role,
            old_data={'name': '编码角色'},
            new_data={'name': '编码角色', 'code': 'new_code'},
        )
        _apply_role_edit(ticket, actor)
        role.refresh_from_db()
        assert role.role_key == 'new_code'


# ---------------------------------------------------------------------------
# 9. _apply_role_delete 测试
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestApplyRoleDelete:
    """_apply_role_delete 各分支：目标缺失、已软删、内置、有用户、正常"""

    def test_when_target_none_then_skip(self):
        """target_role 为 None（幂等跳过），不抛异常"""
        from apps.users.services.ticket_role import _apply_role_delete
        actor = _create_user('del none actor')
        ticket = _make_role_ticket(
            actor, RoleOperation.DELETE,
            target_role=None,
        )
        # target_role 为 None 时幂等跳过
        _apply_role_delete(ticket, actor)

    def test_when_already_deleted_then_skip(self):
        """角色已软删（幂等跳过），不抛异常"""
        from apps.users.services.ticket_role import _apply_role_delete
        actor = _create_user('del already actor')
        role = _create_role('already_deleted', name='已删除角色', is_deleted=True)
        ticket = _make_role_ticket(
            actor, RoleOperation.DELETE,
            target_role=role,
        )
        _apply_role_delete(ticket, actor)
        role.refresh_from_db()
        # 仍为已删除
        assert role.is_deleted is True

    def test_when_builtin_then_raises(self):
        """内置角色删除抛 ValueError"""
        from apps.users.services.ticket_role import _apply_role_delete
        actor = _create_user('del builtin actor')
        role = _create_role('super_admin', name='超管', is_builtin=True)
        ticket = _make_role_ticket(
            actor, RoleOperation.DELETE,
            target_role=role,
        )
        with pytest.raises(ValueError, match='内置角色不可删除'):
            _apply_role_delete(ticket, actor)

    def test_when_users_bound_then_raises(self):
        """角色有用户绑定时删除抛 ValueError"""
        from apps.users.services.ticket_role import _apply_role_delete
        actor = _create_user('del bound actor')
        bound_user = _create_user('del bound user')
        role = _create_role('bound_role', name='有用户角色')
        UserRoleRel.objects.create(
            user=bound_user, role=role, status=GrantStatus.ACTIVE)
        ticket = _make_role_ticket(
            actor, RoleOperation.DELETE,
            target_role=role,
        )
        with pytest.raises(ValueError, match='该角色被'):
            _apply_role_delete(ticket, actor)

    def test_when_normal_delete_then_soft_delete(self):
        """正常软删除：is_deleted=True + deleted_at 非空"""
        from apps.users.services.ticket_role import _apply_role_delete
        actor = _create_user('del normal actor')
        role = _create_role('normal_del_role', name='正常删除角色')
        ticket = _make_role_ticket(
            actor, RoleOperation.DELETE,
            target_role=role,
        )
        _apply_role_delete(ticket, actor)
        role.refresh_from_db()
        assert role.is_deleted is True
        assert role.deleted_at is not None


# ---------------------------------------------------------------------------
# 10. _apply_role_assign_perms 测试
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestApplyRoleAssignPerms:
    """_apply_role_assign_perms 各分支：目标缺失、正常分配"""

    def test_when_target_none_then_raises(self):
        """target_role 为 None 时抛 ValueError"""
        from apps.users.services.ticket_role import _apply_role_assign_perms
        actor = _create_user('perm none actor')
        ticket = _make_role_ticket(
            actor, RoleOperation.ASSIGN_PERMS,
            target_role=None,
            permission_ids=[1, 2],
        )
        with pytest.raises(ValueError, match='目标角色不存在'):
            _apply_role_assign_perms(ticket, actor)

    def test_when_normal_assign_then_calls_service(self):
        """正常权限分配：调用 assign_permissions_to_role 并写审计"""
        from apps.users.services.ticket_role import _apply_role_assign_perms
        actor = _create_user('perm normal actor')
        role = _create_role('perm_role', name='权限分配角色')
        ticket = _make_role_ticket(
            actor, RoleOperation.ASSIGN_PERMS,
            target_role=role,
            permission_ids=[10, 20, 30],
        )
        with patch('apps.users.services.ticket_role.assign_permissions_to_role',
                   return_value=([10, 20], 1)) as mock_assign:
            _apply_role_assign_perms(ticket, actor)
            mock_assign.assert_called_once_with(role, [10, 20, 30], actor)
        # 验证审计日志已写入
        audit = PermissionAuditLog.objects.filter(
            action='ROLE_PERMS_ASSIGN',
            target_type=AuditTargetType.ROLE,
            target_id=role.id,
        ).latest('log_id')
        assert audit.actor == actor

    def test_when_empty_permission_ids_then_calls_service(self):
        """空权限列表也应正常调用（全量清除场景）"""
        from apps.users.services.ticket_role import _apply_role_assign_perms
        actor = _create_user('perm empty actor')
        role = _create_role('empty_perm_role', name='清空权限角色')
        ticket = _make_role_ticket(
            actor, RoleOperation.ASSIGN_PERMS,
            target_role=role,
            permission_ids=[],
        )
        with patch('apps.users.services.ticket_role.assign_permissions_to_role',
                   return_value=([], 0)) as mock_assign:
            _apply_role_assign_perms(ticket, actor)
            mock_assign.assert_called_once_with(role, [], actor)
