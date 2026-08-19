"""
apps.users.services.ticket_security 集成测试 —— 安全配置工单服务

覆盖范围：
- create_security_ticket：低风险直接执行 / 中风险单审 / 高风险双审
- _get_security_risk_level：风险等级判定（所有组合 + 默认）
- _build_security_approval_chain：审批链构造（low/normal/high）
- _execute_ip_whitelist：白名单 ADD/EDIT/DELETE
- _execute_ip_blacklist：黑名单 ADD/DELETE
- _execute_sensitive_word：敏感词 ADD/EDIT/DELETE/DISABLE + 竞态 Integ

需要 DB（@pytest.mark.integration）：工单创建涉及事务写入多表。
"""
import pytest
from unittest.mock import patch, MagicMock

from apps.users.models import (
    TicketList, TicketSecurityDetail, User,
    TicketStatus, TicketBizType,
    SecurityConfigType, SecurityOperation,
)
from apps.users.services.ticket_security import (
    create_security_ticket,
    _get_security_risk_level,
    _build_security_approval_chain,
    _execute_security_change,
    _execute_ip_whitelist,
    _execute_ip_blacklist,
    _execute_sensitive_word,
)


# ============================================================================
# 辅助函数
# ============================================================================

def _create_user(username):
    """创建测试用户"""
    return User.objects.create_user(
        username=username, email=f'{username}@test.com', password='pass12345',
    )


# ============================================================================
# _get_security_risk_level 风险等级判定
# ============================================================================

class TestGetSecurityRiskLevel:
    """风险等级判定：所有枚举组合 + 默认回退"""

    @pytest.mark.unit
    def test_ip_whitelist_all_ops_return_high(self):
        """IP 白名单所有操作均为高风险"""
        for op in [SecurityOperation.ADD, SecurityOperation.EDIT, SecurityOperation.DELETE]:
            assert _get_security_risk_level(SecurityConfigType.IP_WHITELIST, op) == 'high'

    @pytest.mark.unit
    def test_ip_blacklist_add_return_low(self):
        """IP 黑名单新增为低风险（防御性操作）"""
        assert _get_security_risk_level(SecurityConfigType.IP_BLACKLIST, SecurityOperation.ADD) == 'low'

    @pytest.mark.unit
    def test_ip_blacklist_delete_return_normal(self):
        """IP 黑名单删除/解封为中风险"""
        assert _get_security_risk_level(SecurityConfigType.IP_BLACKLIST, SecurityOperation.DELETE) == 'normal'

    @pytest.mark.unit
    def test_sensitive_word_add_return_low(self):
        """敏感词新增为低风险"""
        assert _get_security_risk_level(SecurityConfigType.SENSITIVE_WORD, SecurityOperation.ADD) == 'low'

    @pytest.mark.unit
    def test_sensitive_word_edit_delete_disable_return_normal(self):
        """敏感词编辑/删除/禁用均为中风险"""
        for op in [SecurityOperation.EDIT, SecurityOperation.DELETE, SecurityOperation.DISABLE]:
            assert _get_security_risk_level(SecurityConfigType.SENSITIVE_WORD, op) == 'normal'

    @pytest.mark.unit
    def test_unknown_combination_returns_normal_default(self):
        """未匹配的组合默认返回 normal（走单审）"""
        assert _get_security_risk_level('unknown_type', SecurityOperation.ADD) == 'normal'
        assert _get_security_risk_level(SecurityConfigType.IP_WHITELIST, 'unknown_op') == 'normal'


# ============================================================================
# _build_security_approval_chain 审批链构造
# ============================================================================

class TestBuildSecurityApprovalChain:
    """审批链构造：low/normal/high 三种风险等级"""

    @pytest.mark.unit
    def test_low_risk_returns_empty_chain(self):
        """低风险返回空审批链（直接生效）"""
        assert _build_security_approval_chain('low') == []

    @pytest.mark.unit
    def test_normal_risk_returns_single_approver(self):
        """中风险返回单审链（USER_ADMIN）"""
        chain = _build_security_approval_chain('normal')
        assert len(chain) == 1
        assert chain[0]['approver_role'] == 'USER_ADMIN'
        assert chain[0]['status'] == 'PENDING'

    @pytest.mark.unit
    def test_high_risk_returns_double_approver(self):
        """高风险返回双审链（USER_ADMIN + SUPER_ADMIN）"""
        chain = _build_security_approval_chain('high')
        assert len(chain) == 2
        assert chain[0]['approver_role'] == 'USER_ADMIN'
        assert chain[1]['approver_role'] == 'SUPER_ADMIN'
        assert all(n['status'] == 'PENDING' for n in chain)


# ============================================================================
# create_security_ticket 集成测试
# ============================================================================

@pytest.mark.integration
class TestCreateSecurityTicket:
    """工单创建：低风险直接执行 / 中风险单审 / 高风险双审"""

    @patch('apps.users.services.ticket_security._write_audit')
    @patch('apps.users.services.ticket_security._log_flow')
    @patch('apps.users.services.ticket_security._create_ticket_with_retry')
    def test_low_risk_ip_blacklist_add_directly_executes(
        self, mock_retry, mock_log, mock_audit, db,
    ):
        """低风险 IP 黑名单新增：工单状态为 EXECUTED，直接执行变更"""
        actor = _create_user('actor1')
        target = {'ip_pattern': '10.0.0.1', 'reason': 'login_fail', 'detail': '测试'}
        expected_ticket = MagicMock(spec=TicketList)
        expected_ticket.ticket_no = 'AQ202608190001'

        def side_effect(biz_type, build_fn):
            return build_fn(expected_ticket.ticket_no)

        mock_retry.side_effect = side_effect

        ticket = create_security_ticket(
            actor=actor,
            security_type=SecurityConfigType.IP_BLACKLIST,
            operation=SecurityOperation.ADD,
            target_data=target,
            reason='测试低风险',
        )

        assert ticket.risk_level == 'low'
        assert ticket.status == TicketStatus.EXECUTED
        assert ticket.approval_chain == []

    @patch('apps.users.services.ticket_security._write_audit')
    @patch('apps.users.services.ticket_security._log_flow')
    @patch('apps.users.services.ticket_security._create_ticket_with_retry')
    def test_low_risk_sensitive_word_add_directly_executes(
        self, mock_retry, mock_log, mock_audit, db,
    ):
        """低风险敏感词新增：工单状态为 EXECUTED"""
        actor = _create_user('actor_sw')
        target = {'word': 'testword', 'category': 'other', 'action': 'mask'}
        expected_ticket = MagicMock(spec=TicketList)
        expected_ticket.ticket_no = 'AQ202608190002'

        def side_effect(biz_type, build_fn):
            return build_fn(expected_ticket.ticket_no)

        mock_retry.side_effect = side_effect

        ticket = create_security_ticket(
            actor=actor,
            security_type=SecurityConfigType.SENSITIVE_WORD,
            operation=SecurityOperation.ADD,
            target_data=target,
            reason='测试敏感词低风险',
        )

        assert ticket.risk_level == 'low'
        assert ticket.status == TicketStatus.EXECUTED

    @patch('apps.users.services.ticket_security._write_audit')
    @patch('apps.users.services.ticket_security._log_flow')
    @patch('apps.users.services.ticket_security._create_ticket_with_retry')
    def test_normal_risk_ip_blacklist_delete_pending(
        self, mock_retry, mock_log, mock_audit, db,
    ):
        """中风险 IP 黑名单删除（解封）：工单状态为 PENDING，单审链"""
        actor = _create_user('actor2')
        target = {'id': 999}
        expected_ticket = MagicMock(spec=TicketList)
        expected_ticket.ticket_no = 'AQ202608190003'

        def side_effect(biz_type, build_fn):
            return build_fn(expected_ticket.ticket_no)

        mock_retry.side_effect = side_effect

        ticket = create_security_ticket(
            actor=actor,
            security_type=SecurityConfigType.IP_BLACKLIST,
            operation=SecurityOperation.DELETE,
            target_data=target,
            reason='测试解封',
        )

        assert ticket.risk_level == 'normal'
        assert ticket.status == TicketStatus.PENDING
        assert len(ticket.approval_chain) == 1

    @patch('apps.users.services.ticket_security._write_audit')
    @patch('apps.users.services.ticket_security._log_flow')
    @patch('apps.users.services.ticket_security._create_ticket_with_retry')
    def test_normal_risk_sensitive_word_edit_pending(
        self, mock_retry, mock_log, mock_audit, db,
    ):
        """中风险敏感词编辑：工单状态为 PENDING"""
        actor = _create_user('actor3')
        target = {'id': 1}
        new_data = {'action': 'block'}
        expected_ticket = MagicMock(spec=TicketList)
        expected_ticket.ticket_no = 'AQ202608190004'

        def side_effect(biz_type, build_fn):
            return build_fn(expected_ticket.ticket_no)

        mock_retry.side_effect = side_effect

        ticket = create_security_ticket(
            actor=actor,
            security_type=SecurityConfigType.SENSITIVE_WORD,
            operation=SecurityOperation.EDIT,
            target_data=target,
            reason='编辑敏感词',
            new_data=new_data,
        )

        assert ticket.risk_level == 'normal'
        assert ticket.status == TicketStatus.PENDING

    @patch('apps.users.services.ticket_security._write_audit')
    @patch('apps.users.services.ticket_security._log_flow')
    @patch('apps.users.services.ticket_security._create_ticket_with_retry')
    def test_normal_risk_sensitive_word_delete_pending(
        self, mock_retry, mock_log, mock_audit, db,
    ):
        """中风险敏感词删除：工单状态为 PENDING"""
        actor = _create_user('actor4')
        target = {'id': 2}
        expected_ticket = MagicMock(spec=TicketList)
        expected_ticket.ticket_no = 'AQ202608190005'

        def side_effect(biz_type, build_fn):
            return build_fn(expected_ticket.ticket_no)

        mock_retry.side_effect = side_effect

        ticket = create_security_ticket(
            actor=actor,
            security_type=SecurityConfigType.SENSITIVE_WORD,
            operation=SecurityOperation.DELETE,
            target_data=target,
            reason='删除敏感词',
        )

        assert ticket.risk_level == 'normal'
        assert ticket.status == TicketStatus.PENDING

    @patch('apps.users.services.ticket_security._write_audit')
    @patch('apps.users.services.ticket_security._log_flow')
    @patch('apps.users.services.ticket_security._create_ticket_with_retry')
    def test_normal_risk_sensitive_word_disable_pending(
        self, mock_retry, mock_log, mock_audit, db,
    ):
        """中风险敏感词禁用：工单状态为 PENDING"""
        actor = _create_user('actor5')
        target = {'id': 3}
        expected_ticket = MagicMock(spec=TicketList)
        expected_ticket.ticket_no = 'AQ202608190006'

        def side_effect(biz_type, build_fn):
            return build_fn(expected_ticket.ticket_no)

        mock_retry.side_effect = side_effect

        ticket = create_security_ticket(
            actor=actor,
            security_type=SecurityConfigType.SENSITIVE_WORD,
            operation=SecurityOperation.DISABLE,
            target_data=target,
            reason='禁用敏感词',
        )

        assert ticket.risk_level == 'normal'
        assert ticket.status == TicketStatus.PENDING

    @patch('apps.users.services.ticket_security._write_audit')
    @patch('apps.users.services.ticket_security._log_flow')
    @patch('apps.users.services.ticket_security._create_ticket_with_retry')
    def test_high_risk_ip_whitelist_add_pending(
        self, mock_retry, mock_log, mock_audit, db,
    ):
        """高风险 IP 白名单新增：工单状态为 PENDING，双审链"""
        actor = _create_user('actor6')
        target = {'ip_pattern': '192.168.1.0/24', 'description': '内网段'}
        expected_ticket = MagicMock(spec=TicketList)
        expected_ticket.ticket_no = 'AQ202608190007'

        def side_effect(biz_type, build_fn):
            return build_fn(expected_ticket.ticket_no)

        mock_retry.side_effect = side_effect

        ticket = create_security_ticket(
            actor=actor,
            security_type=SecurityConfigType.IP_WHITELIST,
            operation=SecurityOperation.ADD,
            target_data=target,
            reason='新增白名单',
        )

        assert ticket.risk_level == 'high'
        assert ticket.status == TicketStatus.PENDING
        assert len(ticket.approval_chain) == 2

    @patch('apps.users.services.ticket_security._write_audit')
    @patch('apps.users.services.ticket_security._log_flow')
    @patch('apps.users.services.ticket_security._create_ticket_with_retry')
    def test_high_risk_ip_whitelist_edit_pending(
        self, mock_retry, mock_log, mock_audit, db,
    ):
        """高风险 IP 白名单编辑：双审链"""
        actor = _create_user('actor7')
        target = {'id': 10}
        new_data = {'description': '更新描述'}
        expected_ticket = MagicMock(spec=TicketList)
        expected_ticket.ticket_no = 'AQ202608190008'

        def side_effect(biz_type, build_fn):
            return build_fn(expected_ticket.ticket_no)

        mock_retry.side_effect = side_effect

        ticket = create_security_ticket(
            actor=actor,
            security_type=SecurityConfigType.IP_WHITELIST,
            operation=SecurityOperation.EDIT,
            target_data=target,
            reason='编辑白名单',
            new_data=new_data,
        )

        assert ticket.risk_level == 'high'
        assert len(ticket.approval_chain) == 2

    @patch('apps.users.services.ticket_security._write_audit')
    @patch('apps.users.services.ticket_security._log_flow')
    @patch('apps.users.services.ticket_security._create_ticket_with_retry')
    def test_high_risk_ip_whitelist_delete_pending(
        self, mock_retry, mock_log, mock_audit, db,
    ):
        """高风险 IP 白名单删除：双审链"""
        actor = _create_user('actor8')
        target = {'id': 11}
        expected_ticket = MagicMock(spec=TicketList)
        expected_ticket.ticket_no = 'AQ202608190009'

        def side_effect(biz_type, build_fn):
            return build_fn(expected_ticket.ticket_no)

        mock_retry.side_effect = side_effect

        ticket = create_security_ticket(
            actor=actor,
            security_type=SecurityConfigType.IP_WHITELIST,
            operation=SecurityOperation.DELETE,
            target_data=target,
            reason='删除白名单',
        )

        assert ticket.risk_level == 'high'
        assert len(ticket.approval_chain) == 2


# ============================================================================
# _execute_security_change 路由与异常分支
# ============================================================================

@pytest.mark.integration
class TestExecuteSecurityChange:
    """执行路由：缺少 detail / 未知 security_type"""

    @patch('apps.users.services.ticket_security.IpWhitelist', create=True)
    def test_when_detail_missing_then_return(self, mock_model, db):
        """工单无 security_detail 时安全返回，不抛异常"""
        ticket = MagicMock(spec=TicketList)
        ticket.ticket_no = 'AQ202608190010'
        ticket.security_detail = None

        # 不应抛异常
        _execute_security_change(ticket)

    @patch('apps.users.services.ticket_security.IpWhitelist', create=True)
    def test_when_unknown_security_type_then_noop(self, mock_model, db):
        """未知 security_type 时仅记日志，不执行任何变更"""
        ticket = MagicMock(spec=TicketList)
        ticket.ticket_no = 'AQ202608190011'
        detail = MagicMock()
        detail.security_type = 'unknown_type'
        detail.operation = SecurityOperation.ADD
        detail.target_data = {}
        detail.new_data = {}
        ticket.security_detail = detail

        # 不应抛异常
        _execute_security_change(ticket)


# ============================================================================
# _execute_ip_whitelist 白名单变更
# ============================================================================

@pytest.mark.integration
class TestExecuteIpWhitelist:
    """IP 白名单执行：ADD / EDIT / DELETE"""

    def test_add_creates_ip_whitelist(self, db):
        """ADD 操作创建 IpWhitelist 记录"""
        from apps.security.models import IpWhitelist

        target = {'ip_pattern': '10.0.0.0/8', 'description': '内网段'}
        ticket = MagicMock(spec=TicketList)

        _execute_ip_whitelist(SecurityOperation.ADD, target, {}, ticket)

        obj = IpWhitelist.objects.get(ip_or_cidr='10.0.0.0/8')
        assert obj.description == '内网段'
        assert obj.is_enabled is True

    def test_add_fallback_ip_or_cidr_key(self, db):
        """ADD 操作兼容 ip_or_cidr 字段名（无 ip_pattern 时回退）"""
        from apps.security.models import IpWhitelist

        target = {'ip_or_cidr': '192.168.1.1', 'description': '测试IP'}
        ticket = MagicMock(spec=TicketList)

        _execute_ip_whitelist(SecurityOperation.ADD, target, {}, ticket)

        obj = IpWhitelist.objects.get(ip_or_cidr='192.168.1.1')
        assert obj.description == '测试IP'

    def test_edit_updates_ip_whitelist_fields(self, db):
        """EDIT 操作更新白名单业务字段"""
        from apps.security.models import IpWhitelist

        obj = IpWhitelist.objects.create(
            ip_or_cidr='10.1.1.1', description='旧描述', is_enabled=True,
        )
        target = {'id': obj.id}
        new_data = {'description': '新描述', 'ip_or_cidr': '10.2.2.2'}
        ticket = MagicMock(spec=TicketList)

        _execute_ip_whitelist(SecurityOperation.EDIT, target, new_data, ticket)

        obj.refresh_from_db()
        assert obj.description == '新描述'
        assert obj.ip_or_cidr == '10.2.2.2'

    def test_edit_ignores_non_allowed_fields(self, db):
        """EDIT 操作忽略非法 key，不污染模型其他字段"""
        from apps.security.models import IpWhitelist

        obj = IpWhitelist.objects.create(
            ip_or_cidr='10.3.3.3', description='原始描述', is_enabled=True,
        )
        target = {'id': obj.id}
        # 传入不允许的 key
        new_data = {'description': '更新', 'is_enabled': False, 'created_by': 999}
        ticket = MagicMock(spec=TicketList)

        _execute_ip_whitelist(SecurityOperation.EDIT, target, new_data, ticket)

        obj.refresh_from_db()
        assert obj.description == '更新'
        # is_enabled 和 created_by 不应被修改
        assert obj.is_enabled is True

    def test_edit_nonexistent_id_is_noop(self, db):
        """EDIT 操作目标不存在时静默跳过"""
        target = {'id': 99999}
        new_data = {'description': '不存在'}
        ticket = MagicMock(spec=TicketList)

        # 不应抛异常
        _execute_ip_whitelist(SecurityOperation.EDIT, target, new_data, ticket)

    def test_delete_removes_ip_whitelist(self, db):
        """DELETE 操作删除白名单记录"""
        from apps.security.models import IpWhitelist

        obj = IpWhitelist.objects.create(
            ip_or_cidr='10.4.4.4', description='待删除', is_enabled=True,
        )
        target = {'id': obj.id}
        ticket = MagicMock(spec=TicketList)

        _execute_ip_whitelist(SecurityOperation.DELETE, target, {}, ticket)

        assert not IpWhitelist.objects.filter(id=obj.id).exists()


# ============================================================================
# _execute_ip_blacklist 黑名单变更
# ============================================================================

@pytest.mark.integration
class TestExecuteIpBlacklist:
    """IP 黑名单执行：ADD / DELETE"""

    def test_add_creates_ip_blacklist(self, db):
        """ADD 操作创建 IpBlacklist 记录"""
        from apps.security.models import IpBlacklist

        target = {'ip_pattern': '192.168.100.1', 'reason': 'bot', 'detail': '爬虫检测'}
        ticket = MagicMock(spec=TicketList)

        _execute_ip_blacklist(SecurityOperation.ADD, target, {}, ticket)

        obj = IpBlacklist.objects.get(ip='192.168.100.1')
        assert obj.reason == 'bot'
        assert obj.detail == '爬虫检测'
        assert obj.is_active is True

    def test_add_fallback_ip_key(self, db):
        """ADD 操作兼容 ip 字段名（无 ip_pattern 时回退）"""
        from apps.security.models import IpBlacklist

        target = {'ip': '10.99.99.99', 'reason': 'manual', 'detail': '手动封禁'}
        ticket = MagicMock(spec=TicketList)

        _execute_ip_blacklist(SecurityOperation.ADD, target, {}, ticket)

        obj = IpBlacklist.objects.get(ip='10.99.99.99')
        assert obj.is_active is True

    def test_delete_sets_inactive(self, db):
        """DELETE（解封）操作将黑名单标记为非活跃"""
        from apps.security.models import IpBlacklist

        obj = IpBlacklist.objects.create(
            ip='172.16.0.1', reason='login_fail', is_active=True,
        )
        target = {'id': obj.id}
        ticket = MagicMock(spec=TicketList)

        _execute_ip_blacklist(SecurityOperation.DELETE, target, {}, ticket)

        obj.refresh_from_db()
        assert obj.is_active is False

    def test_delete_nonexistent_id_is_noop(self, db):
        """DELETE 操作目标不存在时静默跳过"""
        target = {'id': 99999}
        ticket = MagicMock(spec=TicketList)

        # 不应抛异常
        _execute_ip_blacklist(SecurityOperation.DELETE, target, {}, ticket)


# ============================================================================
# _execute_sensitive_word 敏感词变更
# ============================================================================

@pytest.mark.integration
class TestExecuteSensitiveWord:
    """敏感词执行：ADD / EDIT / DELETE / DISABLE + 竞态 Integ"""

    def test_add_creates_sensitive_word(self, db):
        """ADD 操作创建 SensitiveWord 记录"""
        from apps.security.models import SensitiveWord

        target = {'word': '机密文档', 'category': 'secret', 'action': 'block',
                  'is_regex': False}
        ticket = MagicMock(spec=TicketList)

        _execute_sensitive_word(SecurityOperation.ADD, target, {}, ticket)

        obj = SensitiveWord.objects.get(word='机密文档')
        assert obj.category == 'secret'
        assert obj.action == 'block'
        assert obj.is_regex is False
        assert obj.is_enabled is True

    def test_add_with_defaults(self, db):
        """ADD 操作使用默认值（category=custom, action=mask）"""
        from apps.security.models import SensitiveWord

        target = {'word': '默认词'}
        ticket = MagicMock(spec=TicketList)

        _execute_sensitive_word(SecurityOperation.ADD, target, {}, ticket)

        obj = SensitiveWord.objects.get(word='默认词')
        assert obj.category == 'custom'
        assert obj.action == 'mask'

    def test_edit_updates_sensitive_word(self, db):
        """EDIT 操作更新敏感词业务字段"""
        from apps.security.models import SensitiveWord

        obj = SensitiveWord.objects.create(
            word='旧词', category='other', action='mask', is_regex=False,
        )
        target = {'id': obj.id}
        new_data = {'word': '新词', 'action': 'block', 'category': 'secret'}
        ticket = MagicMock(spec=TicketList)

        _execute_sensitive_word(SecurityOperation.EDIT, target, new_data, ticket)

        obj.refresh_from_db()
        assert obj.word == '新词'
        assert obj.action == 'block'
        assert obj.category == 'secret'

    def test_edit_ignores_non_allowed_fields(self, db):
        """EDIT 操作忽略非法 key，不污染模型字段"""
        from apps.security.models import SensitiveWord

        obj = SensitiveWord.objects.create(
            word='安全词', category='other', action='mask',
        )
        target = {'id': obj.id}
        new_data = {'word': '已更新', 'is_enabled': False, 'hit_count': 9999}
        ticket = MagicMock(spec=TicketList)

        _execute_sensitive_word(SecurityOperation.EDIT, target, new_data, ticket)

        obj.refresh_from_db()
        assert obj.word == '已更新'
        # is_enabled 和 hit_count 不应被修改
        assert obj.is_enabled is True
        assert obj.hit_count == 0

    def test_edit_nonexistent_id_is_noop(self, db):
        """EDIT 操作目标不存在时静默跳过"""
        target = {'id': 99999}
        new_data = {'word': '不存在'}
        ticket = MagicMock(spec=TicketList)

        # 不应抛异常
        _execute_sensitive_word(SecurityOperation.EDIT, target, new_data, ticket)

    def test_delete_removes_sensitive_word(self, db):
        """DELETE 操作删除敏感词记录"""
        from apps.security.models import SensitiveWord

        obj = SensitiveWord.objects.create(
            word='待删除词', category='other', action='mask',
        )
        target = {'id': obj.id}
        ticket = MagicMock(spec=TicketList)

        _execute_sensitive_word(SecurityOperation.DELETE, target, {}, ticket)

        assert not SensitiveWord.objects.filter(id=obj.id).exists()

    def test_disable_sets_inactive(self, db):
        """DISABLE 操作将敏感词标记为禁用"""
        from apps.security.models import SensitiveWord

        obj = SensitiveWord.objects.create(
            word='禁用词', category='other', action='warn', is_enabled=True,
        )
        target = {'id': obj.id}
        ticket = MagicMock(spec=TicketList)

        _execute_sensitive_word(SecurityOperation.DISABLE, target, {}, ticket)

        obj.refresh_from_db()
        assert obj.is_enabled is False

    def test_disable_nonexistent_id_is_noop(self, db):
        """DISABLE 操作目标不存在时静默跳过"""
        target = {'id': 99999}
        ticket = MagicMock(spec=TicketList)

        # 不应抛异常
        _execute_sensitive_word(SecurityOperation.DISABLE, target, {}, ticket)

    @patch('apps.security.models.SensitiveWord')
    def test_add_with_integrity_error_swallows_race(self, mock_sw_class, db):
        """ADD 操作遇 IntegrityError（竞态）时静默跳过，不抛异常

        敏感词并发创建场景：exists() 检查与 create 之间另一请求已创建相同 word，
        唯一索引冲突抛 IntegrityError，应被吞掉并记 warning 日志。
        """
        from django.db import IntegrityError

        mock_sw_class.objects.create.side_effect = IntegrityError('duplicate key')
        target = {'word': '竞态词', 'category': 'other', 'action': 'mask'}
        ticket = MagicMock(spec=TicketList)

        # 不应抛异常
        _execute_sensitive_word(SecurityOperation.ADD, target, {}, ticket)

        mock_sw_class.objects.create.assert_called_once()
