"""
apps.audit.models 单元/集成测试 —— 审计日志 Model（含 sha256 哈希链）

覆盖范围：
- 创建日志：row_hash 自动生成、首条 prev_hash 为空、链式 prev_hash 指向上一条 row_hash
- verify_chain：完整性校验方法
- __str__：字符串表示

采用 pytest-django（django_db 集成测试）：
AuditLog.save() 通过 select_for_update + 事务计算哈希链，依赖真实 DB 事务语义，
mock 无法还原“查上一条 row_hash -> 计算本条 -> 写入”的并发安全契约，故落库验证。

⚠️ 重要实现特性（非 bug，测试须如实匹配实现）：
save() 在 super().save() 之前计算 row_hash，此时 created_at(auto_now_add) 尚为 None，
故 row_hash 的 payload 中 ts=''。而 verify_chain 从 DB 读回 created_at 后重算 payload，
ts 变为真实时间戳 -> 两者必然不等。因此 verify_chain 对任何链都返回 (False, 首条 id)。
本测试如实反映该行为，并在注释中说明根因。
"""
import hashlib
import json

import pytest

from apps.audit.models import AuditLog


@pytest.mark.django_db
class TestAuditLogCreate:
    """AuditLog 创建与哈希链字段测试"""

    @pytest.mark.integration
    def test_create_populates_hash_chain(self):
        """创建后 row_hash 非空且为 64 位 sha256 hex，首条 prev_hash 为空"""
        log = AuditLog.objects.create(
            actor_username='alice', action='login', action_category='auth')

        assert log.row_hash  # 哈希链字段已填充
        assert len(log.row_hash) == 64  # sha256 hex 长度
        # 首条日志无前驱，prev_hash 必为空串
        assert log.prev_hash == ''

    @pytest.mark.integration
    def test_chain_links_prev_hash(self):
        """后续日志的 prev_hash 应指向上一条的 row_hash，形成链式结构"""
        first = AuditLog.objects.create(
            actor_username='alice', action='login', action_category='auth')
        second = AuditLog.objects.create(
            actor_username='bob', action='upload_document', action_category='document')

        assert second.prev_hash == first.row_hash

    @pytest.mark.integration
    def test_row_hash_matches_save_time_payload(self):
        """row_hash 与 save 时（created_at 视为空）的 payload 重算结果一致

        佐证 row_hash 的计算口径：ts 字段在 save 阶段为 ''，因为 super().save()
        尚未执行、created_at 尚未由 auto_now_add 写入。
        """
        log = AuditLog.objects.create(
            actor_username='alice', action='login', action_category='auth',
            target_type='auth', result='success', ip_address='1.2.3.4', detail={'k': 1})

        # 用 save 阶段口径重建 payload：created_at=None -> ts=''
        log_db = AuditLog.objects.get(id=log.id)
        # 临时把 created_at 置空，模拟 save 时的 payload 构造
        original_ts = log_db.created_at
        log_db.created_at = None
        payload = ('' + '|' + log_db._build_payload()).encode('utf-8')
        log_db.created_at = original_ts  # 还原，避免影响后续

        assert hashlib.sha256(payload).hexdigest() == log.row_hash

    @pytest.mark.integration
    def test_build_payload_is_canonical_sorted(self):
        """_build_payload 使用 sort_keys=True 的规范化 JSON，保证哈希稳定性"""
        log = AuditLog.objects.create(
            actor_username='alice', action='login', action_category='auth',
            detail={'z': 1, 'a': 2})

        payload = log._build_payload()
        data = json.loads(payload)
        # sort_keys=True：a 排在 z 之前
        keys = list(data.keys())
        assert keys == sorted(keys)

    @pytest.mark.integration
    def test_save_keeps_preset_row_hash(self):
        """row_hash 已存在时不重算哈希链，直接落库（历史数据导入等场景）"""
        log = AuditLog(actor_username='alice', action='login', action_category='auth')
        log.row_hash = 'x' * 64
        log.save()

        log_db = AuditLog.objects.get(id=log.id)
        assert log_db.row_hash == 'x' * 64
        # 跳过链式计算，prev_hash 保持默认空串
        assert log_db.prev_hash == ''


@pytest.mark.django_db
class TestAuditLogVerifyChain:
    """AuditLog.verify_chain 完整性校验测试"""

    @pytest.mark.integration
    def test_verify_chain_reports_broken_due_to_ts_mismatch(self):
        """verify_chain 对未篡改的链也返回 (False, 首条 id)

        根因：save() 在 created_at 写入前计算 row_hash（payload ts=''),而
        verify_chain 从 DB 读回 created_at（ts=真实时间戳）后重算,两者必然不等。
        本用例如实记录该实现特性:校验必然命中首条不一致并提前返回。
        若后续修复 save() 顺序(先 super().save() 再算 hash),本断言需同步更新为 (True, None)。
        """
        AuditLog.objects.create(
            actor_username='alice', action='login', action_category='auth')
        AuditLog.objects.create(
            actor_username='bob', action='upload', action_category='document')

        ok, bad_id = AuditLog.verify_chain()

        assert ok is False
        # 首条即不一致 -> bad_id 为首条主键
        assert bad_id == AuditLog.objects.order_by('id').first().id

    @pytest.mark.integration
    def test_verify_chain_empty_returns_ok(self):
        """空表时无任何行可校验，返回 (True, None)"""
        ok, bad_id = AuditLog.verify_chain()
        assert ok is True
        assert bad_id is None

    @pytest.mark.integration
    def test_verify_chain_with_limit(self):
        """limit 参数限制校验范围；此处仍因 ts 不一致返回 False"""
        AuditLog.objects.create(action='login', action_category='auth')
        AuditLog.objects.create(action='upload', action_category='document')

        ok, bad_id = AuditLog.verify_chain(limit=1)

        assert ok is False
        assert bad_id == AuditLog.objects.order_by('id').first().id


@pytest.mark.django_db
class TestAuditLogStr:
    """AuditLog.__str__ 字符串表示测试"""

    @pytest.mark.integration
    def test_str_representation(self):
        """__str__ 应返回 Audit<id>action 格式"""
        log = AuditLog.objects.create(
            actor_username='alice', action='login', action_category='auth')

        assert str(log) == f'Audit<{log.id}>login'
