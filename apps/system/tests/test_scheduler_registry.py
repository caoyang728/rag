"""
apps.system 定时任务调度模块单元测试 —— 注册表 / cron 解析 / 动态调度器

覆盖范围（纯逻辑，mock 隔离 DB 与 Celery 依赖）：
- scheduler_registry：schedule_key / is_schedule_key / validate_cron /
  parse_cron_fields / build_crontab / serialize / parse / normalize /
  compute_schedule_change_summary / default_schedule_dict / load_schedule_snapshot
- schedulers.SystemConfigScheduler：热更新重建 / 保留 last_run_at / 停用移除

Mock 策略：load_schedule_snapshot 依赖 SystemConfig 表，测试中 patch 掉，
保证纯单元测试无 DB 依赖，可快速反馈。
"""
import pytest

from apps.system.scheduler_registry import (
    SCHEDULE_KEY_PREFIX,
    SCHEDULED_TASKS,
    build_crontab,
    build_schedule_from_snapshot,
    compute_schedule_change_summary,
    default_schedule_dict,
    humanize_cron,
    is_schedule_key,
    load_schedule_snapshot,
    normalize_schedule_value,
    parse_cron_fields,
    parse_schedule_value,
    schedule_key,
    serialize_schedule,
    validate_cron,
)
from apps.system.schedulers import SystemConfigScheduler


# ============================================================================
# key 生成与识别
# ============================================================================
class TestScheduleKey:
    """SCHEDULE_ 前缀 key 生成与识别"""

    @pytest.mark.unit
    def test_schedule_key_uppercase_with_prefix(self):
        assert schedule_key('system-metrics-daily') == 'SCHEDULE_SYSTEM-METRICS-DAILY'

    @pytest.mark.unit
    def test_is_schedule_key_true_for_prefixed(self):
        assert is_schedule_key('SCHEDULE_QUEUE-DEPTH-SNAPSHOT') is True

    @pytest.mark.unit
    def test_is_schedule_key_false_for_plain_key(self):
        assert is_schedule_key('LLM_TIMEOUT') is False

    @pytest.mark.unit
    def test_is_schedule_key_false_for_empty(self):
        assert is_schedule_key('') is False


# ============================================================================
# cron 校验
# ============================================================================
class TestValidateCron:
    """5 段 cron 表达式的语法与取值范围校验"""

    @pytest.mark.unit
    def test_validate_cron_valid_daily(self):
        validate_cron('0 2 * * *')  # 每日 02:00，不应抛异常

    @pytest.mark.unit
    def test_validate_cron_valid_wildcard_and_step(self):
        validate_cron('*/5 * * * *')
        validate_cron('30 */2 * * *')
        validate_cron('0 5 * * 1')

    @pytest.mark.unit
    def test_validate_cron_valid_range_and_list(self):
        validate_cron('0 1-3 15 6 0,2,4')

    @pytest.mark.unit
    def test_validate_cron_when_wrong_field_count_then_raises(self):
        with pytest.raises(ValueError):
            validate_cron('0 2 * *')  # 4 段
        with pytest.raises(ValueError):
            validate_cron('0 2 * * * *')  # 6 段

    @pytest.mark.unit
    def test_validate_cron_when_hour_out_of_range_then_raises(self):
        with pytest.raises(ValueError):
            validate_cron('0 24 * * *')
        with pytest.raises(ValueError):
            validate_cron('0 -1 * * *')

    @pytest.mark.unit
    def test_validate_cron_when_minute_out_of_range_then_raises(self):
        with pytest.raises(ValueError):
            validate_cron('60 2 * * *')

    @pytest.mark.unit
    def test_validate_cron_when_day_of_week_out_of_range_then_raises(self):
        with pytest.raises(ValueError):
            validate_cron('0 2 * * 7')  # 周只允许 0-6（0=周日）

    @pytest.mark.unit
    def test_validate_cron_when_bad_step_then_raises(self):
        with pytest.raises(ValueError):
            validate_cron('*/0 * * * *')
        with pytest.raises(ValueError):
            validate_cron('1/0 * * * *')

    @pytest.mark.unit
    def test_validate_cron_when_range_inverted_then_raises(self):
        with pytest.raises(ValueError):
            validate_cron('0 5-2 * * *')


class TestParseCronFields:
    """cron 分字段拆解"""

    @pytest.mark.unit
    def test_parse_cron_fields_returns_field_dict(self):
        assert parse_cron_fields('30 */2 * * 1') == {
            'minute': '30', 'hour': '*/2', 'day_of_month': '*',
            'month': '*', 'day_of_week': '1',
        }


class TestHumanizeCron:
    """cron 中文解释：常见模式翻译成人话，复杂表达式兜底保留"""

    @pytest.mark.unit
    def test_humanize_daily_fixed_time(self):
        assert humanize_cron('0 2 * * *') == '每天 02:00 执行一次'

    @pytest.mark.unit
    def test_humanize_interval_minutes(self):
        assert humanize_cron('*/5 * * * *') == '每 5 分钟执行一次'

    @pytest.mark.unit
    def test_humanize_fixed_hour_step_minutes(self):
        assert humanize_cron('*/5 1 * * *') == '每天 01 点内每 5 分钟执行一次'

    @pytest.mark.unit
    def test_humanize_fixed_hour_step_minutes_weekly(self):
        assert humanize_cron('*/5 1 * * 1') == '每周周一 01 点内每 5 分钟执行一次'

    @pytest.mark.unit
    def test_humanize_fixed_date_and_weekday(self):
        assert humanize_cron('0 2 1 1 1') == '每年 1 月 1 日 且为周一 02:00 执行一次'

    @pytest.mark.unit
    def test_humanize_hourly_fixed_minute(self):
        assert humanize_cron('15 * * * *') == '每小时的第 15 分钟执行一次'

    @pytest.mark.unit
    def test_humanize_interval_hours_on_hour(self):
        assert humanize_cron('0 */2 * * *') == '每 2 小时执行一次'

    @pytest.mark.unit
    def test_humanize_interval_hours_with_minute(self):
        assert humanize_cron('30 */2 * * *') == '每 2 小时的第 30 分钟执行一次'

    @pytest.mark.unit
    def test_humanize_weekly(self):
        assert humanize_cron('0 5 * * 1') == '每周周一 05:00 执行一次'

    @pytest.mark.unit
    def test_humanize_weekly_multi_days(self):
        assert humanize_cron('0 5 * * 1,3,5') == '每周周一、周三、周五 05:00 执行一次'

    @pytest.mark.unit
    def test_humanize_monthly(self):
        assert humanize_cron('0 12 1 * *') == '每月 1 日 12:00 执行一次'

    @pytest.mark.unit
    def test_humanize_yearly(self):
        assert humanize_cron('0 2 1 6 *') == '每年 6 月 1 日 02:00 执行一次'

    @pytest.mark.unit
    def test_humanize_when_complex_then_keeps_raw(self):
        # 无法归类的复杂表达式兜底保留原始 cron，不丢失信息
        assert humanize_cron('0 1-3 15 6 0,2,4') == 'cron 表达式：0 1-3 15 6 0,2,4'

    @pytest.mark.unit
    def test_humanize_when_bad_cron_then_returns_raw(self):
        assert humanize_cron('bad') == 'bad'


# ============================================================================
# 序列化 / 解析 / 规范化
# ============================================================================
class TestScheduleValue:
    """调度配置值的序列化与解析"""

    @pytest.mark.unit
    def test_serialize_schedule_fixed_key_order(self):
        assert serialize_schedule('0 2 * * *', True) == '{"cron": "0 2 * * *", "enabled": true}'

    @pytest.mark.unit
    def test_parse_schedule_value_roundtrip(self):
        assert parse_schedule_value(serialize_schedule('*/5 * * * *', False)) == {
            'cron': '*/5 * * * *', 'enabled': False,
        }

    @pytest.mark.unit
    def test_parse_schedule_value_when_bad_json_then_raises(self):
        with pytest.raises(ValueError):
            parse_schedule_value('not-json')

    @pytest.mark.unit
    def test_parse_schedule_value_when_bad_cron_then_raises(self):
        with pytest.raises(ValueError):
            parse_schedule_value('{"cron": "0 99 * * *", "enabled": true}')

    @pytest.mark.unit
    def test_normalize_schedule_value_normalizes_format(self):
        # 输入为 dict 且键序不同，输出仍是固定键序 JSON
        assert normalize_schedule_value({'enabled': True, 'cron': '30 2 * * *'}) == \
            '{"cron": "30 2 * * *", "enabled": true}'

    @pytest.mark.unit
    def test_normalize_schedule_value_when_invalid_then_raises(self):
        with pytest.raises(ValueError):
            normalize_schedule_value({'cron': 'bad', 'enabled': True})


class TestComputeScheduleChangeSummary:
    """调度变更摘要（cron / 启停）"""

    @pytest.mark.unit
    def test_change_summary_when_cron_changed(self):
        old = serialize_schedule('0 2 * * *', True)
        new = serialize_schedule('30 2 * * *', True)
        import json
        summary = json.loads(compute_schedule_change_summary(old, new))
        assert summary == {
            'schedule': {
                'cron': {
                    'old': '0 2 * * *',
                    'new': '30 2 * * *',
                    'old_desc': '每天 02:00 执行一次',
                    'new_desc': '每天 02:30 执行一次',
                }
            }
        }

    @pytest.mark.unit
    def test_change_summary_when_enabled_changed(self):
        old = serialize_schedule('0 2 * * *', True)
        new = serialize_schedule('0 2 * * *', False)
        import json
        summary = json.loads(compute_schedule_change_summary(old, new))
        assert summary == {'schedule': {'enabled': {'old': True, 'new': False}}}

    @pytest.mark.unit
    def test_change_summary_when_no_change_then_empty(self):
        assert compute_schedule_change_summary(
            serialize_schedule('0 2 * * *', True),
            serialize_schedule('0 2 * * *', True)) == ''


# ============================================================================
# 快照与默认调度
# ============================================================================
class TestScheduleSnapshot:
    """默认调度与快照构建（mock SystemConfig 读取）"""

    @pytest.mark.unit
    def test_default_schedule_dict_contains_all_tasks(self):
        schedule = default_schedule_dict()
        assert set(schedule.keys()) == {t['name'] for t in SCHEDULED_TASKS}

    @pytest.mark.unit
    def test_default_schedule_cron_matches_registry(self, monkeypatch):
        # 无 DB 时快照回退注册表默认值：patch 掉 models.SystemConfig 使其读取抛异常
        monkeypatch.setattr('apps.system.models.SystemConfig', None, raising=False)
        snapshot = load_schedule_snapshot()
        for t in SCHEDULED_TASKS:
            assert snapshot[t['name']]['cron'] == t['cron']
            assert snapshot[t['name']]['task'] == t['task']

    @pytest.mark.unit
    def test_build_schedule_from_snapshot_skips_disabled(self):
        snapshot = {
            'a': {'task': 't.a', 'cron': '0 2 * * *', 'enabled': True},
            'b': {'task': 't.b', 'cron': '0 3 * * *', 'enabled': False},
        }
        schedule = build_schedule_from_snapshot(snapshot)
        assert 'a' in schedule
        assert 'b' not in schedule

    @pytest.mark.unit
    def test_build_crontab_matches_celery_semantics(self):
        cron = build_crontab('30 */2 * * 1')
        # celery crontab 会把步长表达式展开为具体取值集合
        assert 0 in cron.hour and 2 in cron.hour and 22 in cron.hour
        assert cron.day_of_week == {1}  # 周 1（周一）


# ============================================================================
# SystemConfigScheduler 动态调度器
# ============================================================================
class _FakeEntry:
    """带 update 的最小 entry 替身：验证热更新时原 entry 引用被保留（last_run_at 不丢）"""

    def __init__(self, *args, **kwargs):
        self.updated = False

    def update(self, other):
        self.updated = True


class TestSystemConfigScheduler:
    """热更新：快照变化重建、无变化跳过、停用移除、保留 last_run_at"""

    def _make_scheduler(self, monkeypatch, schedule=None):
        """构造最小可用的 scheduler 实例（绕过 Celery app 依赖）"""
        from types import SimpleNamespace

        sched = SystemConfigScheduler.__new__(SystemConfigScheduler)
        sched.app = SimpleNamespace(conf=SimpleNamespace())
        # PersistentScheduler.schedule 是 property setter，依赖 _store 持久化 dict
        sched._store = {}
        sched.schedule = schedule or {}
        sched._last_snapshot = None
        # 新增分支用 SimpleNamespace 构造；已有 entry 的 update 由 _FakeEntry 承担
        sched.Entry = SimpleNamespace
        monkeypatch.setattr(sched, 'sync', lambda: None)
        monkeypatch.setattr(sched, 'install_default_entries', lambda s: None)
        return sched

    @pytest.mark.unit
    def test_reload_applies_new_snapshot(self, monkeypatch):
        sched = self._make_scheduler(monkeypatch)
        snapshot = {'job-a': {'task': 't.a', 'cron': '0 2 * * *', 'enabled': True}}
        monkeypatch.setattr(
            'apps.system.scheduler_registry.load_schedule_snapshot',
            lambda: snapshot,
        )
        sched._reload_from_config()
        assert 'job-a' in sched.schedule
        assert sched._last_snapshot == snapshot

    @pytest.mark.unit
    def test_reload_when_snapshot_unchanged_then_skips(self, monkeypatch):
        sched = self._make_scheduler(monkeypatch)
        snapshot = {'job-a': {'task': 't.a', 'cron': '0 2 * * *', 'enabled': True}}
        monkeypatch.setattr(
            'apps.system.scheduler_registry.load_schedule_snapshot',
            lambda: snapshot,
        )
        sched._apply_snapshot(snapshot)
        calls = []
        monkeypatch.setattr(sched, '_apply_snapshot', lambda s: calls.append(s))
        sched._reload_from_config()
        assert calls == []  # 无变化不重建

    @pytest.mark.unit
    def test_reload_when_snapshot_fails_then_keeps_current(self, monkeypatch):
        sched = self._make_scheduler(monkeypatch)
        sched.schedule = {'old': 'entry'}
        def boom():
            raise RuntimeError('db down')
        monkeypatch.setattr('apps.system.scheduler_registry.load_schedule_snapshot', boom)
        sched._reload_from_config()
        assert sched.schedule == {'old': 'entry'}

    @pytest.mark.unit
    def test_apply_snapshot_removes_disabled_tasks(self, monkeypatch):
        sched = self._make_scheduler(monkeypatch)
        sched.schedule = {'job-a': _FakeEntry(), 'job-b': _FakeEntry()}
        snapshot = {'job-b': {'task': 't.b', 'cron': '0 3 * * *', 'enabled': True}}
        sched._apply_snapshot(snapshot)
        assert 'job-a' not in sched.schedule  # 已从快照移除
        assert 'job-b' in sched.schedule

    @pytest.mark.unit
    def test_apply_snapshot_preserves_entry_last_run(self, monkeypatch):
        # 复用原 entry 引用时，last_run_at 等状态保留（entry.update 内部行为）
        sched = self._make_scheduler(monkeypatch)
        existing = _FakeEntry()
        sched.schedule = {'job-a': existing}
        snapshot = {'job-a': {'task': 't.a', 'cron': '30 2 * * *', 'enabled': True}}
        sched._apply_snapshot(snapshot)
        assert sched.schedule['job-a'] is existing  # 仍是原 entry 对象
        assert existing.updated is True
