"""
apps.users.audit_service 单元/集成测试 —— 统一权限审计日志写入服务

覆盖范围：
- write_audit：只 INSERT 不删、写入失败不阻断主业务（审计可丢、业务不可丢）
- audit_action 装饰器：成功写 SUCCESS、异常写 FAIL 并 re-raise、before/after 快照捕获
- AuditContext 上下文管理器：正常退出写 SUCCESS、异常退出写 FAIL 并 re-raise
- _safe_snapshot：快照回调异常时兜底返回 None，不污染审计
- extract_request_meta：X-Forwarded-For / REMOTE_ADDR 解析、UA 截断

测试分层：
- 纯单元测试（mock write_audit / PermissionAuditLog）：验证装饰器/上下文管理器的控制流
- DB 集成测试（django_db）：验证 write_audit 真实写入审计表且只追加不删
"""
import pytest
from unittest.mock import patch, MagicMock

from apps.users import audit_service
from apps.users.audit_service import (
    write_audit, audit_action, AuditContext, AuditAction,
    extract_request_meta, _safe_snapshot,
)
from apps.users.models import (
    PermissionAuditLog, AuditTargetType, ScopeType, User, Role,
)


# ============================================================================
# 纯单元测试：write_audit 失败兜底（不阻断主业务）
# ============================================================================
class TestWriteAuditFailureHandling:
    """write_audit 写入失败时仅记日志、不抛异常、返回 None"""

    @pytest.mark.unit
    def test_write_failure_returns_none_and_no_raise(self):
        """DB 异常时不向上抛出，返回 None，保证主业务不被审计失败阻断"""
        with patch.object(PermissionAuditLog.objects, 'create',
                          side_effect=RuntimeError('db down')):
            # 不应抛异常
            result = write_audit(
                actor=None, action=AuditAction.LOGIN_SUCCESS,
                target_type=AuditTargetType.LOGIN,
            )
            assert result is None

    @pytest.mark.unit
    def test_write_failure_with_actor_object(self):
        """actor 为对象时失败兜底仍工作，getattr(actor,'id',None) 不应抛错"""
        actor = MagicMock()
        actor.id = 5
        with patch.object(PermissionAuditLog.objects, 'create',
                          side_effect=RuntimeError('db down')):
            result = write_audit(
                actor=actor, action=AuditAction.LOGIN_FAIL,
                target_type=AuditTargetType.LOGIN,
            )
            assert result is None

    @pytest.mark.unit
    def test_ip_address_empty_string_normalized_to_none(self):
        """空 IP 归一为 None，适配 GenericIPAddressField 不接受空串的约束"""
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch.object(PermissionAuditLog.objects, 'create', side_effect=capture):
            write_audit(
                actor=None, action=AuditAction.LOGIN_SUCCESS,
                target_type=AuditTargetType.LOGIN,
                ip_address='',  # 空串应归一为 None
                user_agent='',  # 空串 UA 保持空串
            )
            assert captured['ip_address'] is None
            assert captured['user_agent'] == ''


# ============================================================================
# DB 集成测试：write_audit 真实写入审计表（只 INSERT 不删）
# ============================================================================
@pytest.mark.django_db
class TestWriteAuditDB:
    """write_audit 真实写库行为验证（只追加、永不删）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入操作者用户"""
        self.actor = User.objects.create_user(
            username='auditor', email='auditor@test.com', password='pass12345')

    def test_write_audit_success_creates_log(self):
        """成功写入返回 PermissionAuditLog 实例，记录字段完整"""
        log = write_audit(
            actor=self.actor, action=AuditAction.ROLE_GRANT,
            target_type=AuditTargetType.USER, target_id=10,
            target_user=self.actor,
            scope_type=ScopeType.TEAM, scope_id=20,
            before={'role': 'viewer'}, after={'role': 'contributor'},
            result='SUCCESS', ip_address='10.0.0.1', user_agent='curl/8',
        )
        assert log is not None
        assert log.pk is not None
        log.refresh_from_db()
        assert log.action == AuditAction.ROLE_GRANT
        assert log.target_type == AuditTargetType.USER
        assert log.target_id == 10
        assert log.actor_id == self.actor.id
        assert log.target_user_id == self.actor.id
        assert log.scope_type == ScopeType.TEAM
        assert log.scope_id == 20
        assert log.before_snapshot == {'role': 'viewer'}
        assert log.after_snapshot == {'role': 'contributor'}
        assert log.result == 'SUCCESS'
        assert log.ip_address == '10.0.0.1'
        assert log.user_agent == 'curl/8'

    def test_write_audit_actor_none_allowed(self):
        """actor 为 None（系统自动任务/匿名登录失败）应允许写入"""
        log = write_audit(
            actor=None, action=AuditAction.LOGIN_FAIL,
            target_type=AuditTargetType.LOGIN,
        )
        assert log is not None
        assert log.actor_id is None

    def test_audit_log_never_deleted_by_service(self):
        """审计日志只追加不删：连续写入多条后总数只增不减"""
        for i in range(3):
            write_audit(
                actor=self.actor, action=AuditAction.TICKET_CREATE,
                target_type=AuditTargetType.TICKET, target_id=i,
            )
        assert PermissionAuditLog.objects.filter(
            action=AuditAction.TICKET_CREATE
        ).count() == 3
        # 服务层无 delete/update 调用，记录数保持稳定
        assert PermissionAuditLog.objects.filter(
            action=AuditAction.TICKET_CREATE
        ).count() == 3


# ============================================================================
# audit_action 装饰器：成功写 SUCCESS、异常写 FAIL 并 re-raise
# ============================================================================
class TestAuditActionDecorator:
    """audit_action 装饰器控制流测试（mock write_audit 隔离 DB）"""

    @pytest.mark.unit
    def test_success_writes_success_audit(self):
        """被装饰函数成功时：捕获 before/after 快照，写 SUCCESS 审计"""
        captured_calls = []

        def fake_write(**kwargs):
            captured_calls.append(kwargs)

        @audit_action(
            action=AuditAction.ROLE_GRANT,
            target_type=AuditTargetType.USER,
            target_id_arg='target_id',
            before_fn=lambda a, k: {'old': 'viewer'},
            after_fn=lambda a, k: {'new': 'contributor'},
        )
        def grant_role(actor, target_id):
            return 'ok'

        with patch.object(audit_service, 'write_audit', side_effect=fake_write):
            result = grant_role(MagicMock(id=1), target_id=42)

        assert result == 'ok'
        # 成功路径只写一次 SUCCESS 审计
        assert len(captured_calls) == 1
        audit_kwargs = captured_calls[0]
        assert audit_kwargs['action'] == AuditAction.ROLE_GRANT
        assert audit_kwargs['target_type'] == AuditTargetType.USER
        assert audit_kwargs['target_id'] == 42
        assert audit_kwargs['result'] == 'SUCCESS'
        assert audit_kwargs['before'] == {'old': 'viewer'}
        assert audit_kwargs['after'] == {'new': 'contributor'}

    @pytest.mark.unit
    def test_failure_writes_fail_and_reraise(self):
        """被装饰函数异常时：仅用 before 写 FAIL（after 不可信不捕获），并 re-raise"""
        captured_calls = []

        def fake_write(**kwargs):
            captured_calls.append(kwargs)

        @audit_action(
            action=AuditAction.ROLE_REVOKE,
            target_type=AuditTargetType.USER,
            before_fn=lambda a, k: {'old': 'contributor'},
        )
        def revoke_role(actor, target_id):
            raise ValueError('permission denied')

        with patch.object(audit_service, 'write_audit', side_effect=fake_write):
            with pytest.raises(ValueError, match='permission denied'):
                revoke_role(MagicMock(id=1), target_id=42)

        # 失败路径只写一次 FAIL 审计，不调用 after_fn
        assert len(captured_calls) == 1
        audit_kwargs = captured_calls[0]
        assert audit_kwargs['result'] == 'FAIL'
        assert audit_kwargs['before'] == {'old': 'contributor'}
        # 失败态 after 不可信，不写入
        assert audit_kwargs.get('after') is None

    @pytest.mark.unit
    def test_actor_extracted_from_request_object(self):
        """第一参数为 request（含 .user）时，actor 从 request.user 提取"""
        captured = []

        def fake_write(**kwargs):
            captured.append(kwargs)

        actor_user = MagicMock(spec=User)
        actor_user.id = 99

        @audit_action(action=AuditAction.NODE_MOVE, target_type=AuditTargetType.KNOWLEDGE_NODE)
        def move_node(request):
            return 'done'

        fake_request = MagicMock()
        fake_request.user = actor_user

        with patch.object(audit_service, 'write_audit', side_effect=fake_write):
            move_node(fake_request)

        assert captured[0]['actor'] is actor_user

    @pytest.mark.unit
    def test_anonymous_actor_recorded_as_none(self):
        """AnonymousUser（非 User 实例）不计为合法 actor，记为 None"""
        captured = []

        def fake_write(**kwargs):
            captured.append(kwargs)

        @audit_action(action=AuditAction.NODE_MOVE, target_type=AuditTargetType.KNOWLEDGE_NODE)
        def move_node(request):
            return 'done'

        fake_request = MagicMock()
        # AnonymousUser 不是 User 实例
        fake_request.user = MagicMock()

        with patch.object(audit_service, 'write_audit', side_effect=fake_write):
            move_node(fake_request)

        assert captured[0]['actor'] is None


# ============================================================================
# _safe_snapshot：快照回调异常时兜底返回 None
# ============================================================================
class TestSafeSnapshot:
    """_safe_snapshot：快照回调失败时不影响主业务，返回 None"""

    @pytest.mark.unit
    def test_none_fn_returns_none(self):
        """非可调用对象返回 None"""
        assert _safe_snapshot(None, (), {}) is None

    @pytest.mark.unit
    def test_dict_snapshot_returned(self):
        """正常返回 dict 时原样返回"""
        def fn(args, kwargs):
            return {'key': 'value'}
        assert _safe_snapshot(fn, (), {}) == {'key': 'value'}

    @pytest.mark.unit
    def test_non_dict_returns_none(self):
        """返回非 dict 时返回 None（审计快照必须是 dict）"""
        def fn(args, kwargs):
            return 'not a dict'
        assert _safe_snapshot(fn, (), {}) is None

    @pytest.mark.unit
    def test_exception_returns_none(self):
        """回调抛异常时兜底返回 None，不向上传播"""
        def fn(args, kwargs):
            raise RuntimeError('snapshot failed')
        assert _safe_snapshot(fn, (), {}) is None


# ============================================================================
# AuditContext 上下文管理器：正常退出 SUCCESS、异常退出 FAIL 并 re-raise
# ============================================================================
class TestAuditContext:
    """AuditContext 上下文管理器控制流测试（mock write_audit 隔离 DB）"""

    @pytest.mark.unit
    def test_normal_exit_writes_success(self):
        """with 块正常退出：写 SUCCESS，含已设置的 before/after 快照"""
        captured = []

        def fake_write(**kwargs):
            captured.append(kwargs)

        with patch.object(audit_service, 'write_audit', side_effect=fake_write):
            with AuditContext(
                actor=MagicMock(id=1), action=AuditAction.NODE_MOVE,
                target_type=AuditTargetType.KNOWLEDGE_NODE, target_id=5,
            ) as ctx:
                ctx.set_before({'parent': 'old'})
                ctx.set_after({'parent': 'new'})

        assert len(captured) == 1
        assert captured[0]['result'] == 'SUCCESS'
        assert captured[0]['before'] == {'parent': 'old'}
        assert captured[0]['after'] == {'parent': 'new'}
        assert captured[0]['target_id'] == 5

    @pytest.mark.unit
    def test_exception_exit_writes_fail_and_reraise(self):
        """with 块异常退出：写 FAIL（仅 before），re-raise 原异常"""
        captured = []

        def fake_write(**kwargs):
            captured.append(kwargs)

        with patch.object(audit_service, 'write_audit', side_effect=fake_write):
            with pytest.raises(ValueError, match='boom'):
                with AuditContext(
                    actor=MagicMock(id=1), action=AuditAction.NODE_DELETE,
                    target_type=AuditTargetType.KNOWLEDGE_NODE, target_id=5,
                ) as ctx:
                    ctx.set_before({'node': 'exists'})
                    raise ValueError('boom')

        assert len(captured) == 1
        assert captured[0]['result'] == 'FAIL'
        assert captured[0]['before'] == {'node': 'exists'}
        # 异常态 after 未设置，应为 None
        assert captured[0].get('after') is None

    @pytest.mark.unit
    def test_set_before_after_chainable(self):
        """set_before / set_after 返回 self 支持链式调用"""
        ctx = AuditContext(
            actor=None, action=AuditAction.NODE_MOVE,
            target_type=AuditTargetType.KNOWLEDGE_NODE,
        )
        # 链式调用应返回 ctx 自身
        assert ctx.set_before({'a': 1}) is ctx
        assert ctx.set_after({'b': 2}) is ctx

    @pytest.mark.unit
    def test_set_before_ignores_non_dict(self):
        """非 dict 快照被忽略，保持原 None 值"""
        ctx = AuditContext(
            actor=None, action=AuditAction.NODE_MOVE,
            target_type=AuditTargetType.KNOWLEDGE_NODE,
        )
        ctx.set_before('not a dict')
        assert ctx._before is None


# ============================================================================
# extract_request_meta：IP/UA 解析
# ============================================================================
class TestExtractRequestMeta:
    """extract_request_meta：从 request 提取 (ip, ua)"""

    @pytest.mark.unit
    def test_xff_header_takes_first_ip(self):
        """X-Forwarded-For 取第一个值（反向代理场景下的真实客户端 IP）"""
        request = MagicMock()
        request.META = {
            'HTTP_X_FORWARDED_FOR': '203.0.113.1, 10.0.0.1, 10.0.0.2',
            'HTTP_USER_AGENT': 'Mozilla/5.0',
        }
        ip, ua = extract_request_meta(request)
        assert ip == '203.0.113.1'
        assert ua == 'Mozilla/5.0'

    @pytest.mark.unit
    def test_remote_addr_fallback(self):
        """无 X-Forwarded-For 时回退到 REMOTE_ADDR"""
        request = MagicMock()
        request.META = {
            'REMOTE_ADDR': '10.0.0.5',
            'HTTP_USER_AGENT': 'curl/8',
        }
        ip, ua = extract_request_meta(request)
        assert ip == '10.0.0.5'
        assert ua == 'curl/8'

    @pytest.mark.unit
    def test_empty_ip_normalized_to_none(self):
        """空 IP 归一为 None，适配 GenericIPAddressField 可空约束"""
        request = MagicMock()
        request.META = {'HTTP_USER_AGENT': ''}
        ip, ua = extract_request_meta(request)
        assert ip is None
        assert ua == ''

    @pytest.mark.unit
    def test_user_agent_truncated_to_512(self):
        """UA 截断至 512 字符，对齐 PermissionAuditLog.user_agent max_length"""
        request = MagicMock()
        long_ua = 'x' * 1000
        request.META = {
            'REMOTE_ADDR': '10.0.0.1',
            'HTTP_USER_AGENT': long_ua,
        }
        ip, ua = extract_request_meta(request)
        assert len(ua) == 512

    @pytest.mark.unit
    def test_missing_user_agent_returns_empty_string(self):
        """无 UA header 时返回空串"""
        request = MagicMock()
        request.META = {'REMOTE_ADDR': '10.0.0.1'}
        ip, ua = extract_request_meta(request)
        assert ua == ''
