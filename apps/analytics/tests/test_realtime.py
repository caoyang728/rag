"""
apps.analytics.services.realtime_service 单元测试 —— Redis 实时指标 & 队列深度操作封装

覆盖范围：
- _get_redis：REDIS_URL 优先 / 环境变量降级 两种连接构建
- get_redis_safe：健康检查 TTL 缓存（间隔内不 ping / 过期 ping / 连接失效重建）
- update_queue_depth：Redis LLEN → Redis current 键 + PG QueueDepthLog 批量落库、
  worker 数量获取与失败降级、bulk_create 冲突静默
- get_queue_depth_snapshot：current 键优先 / LLEN 降级 / worker 状态聚合
- increment_realtime_metrics：缓存命中 / 正常请求 / 失败计数三种分支
- get_realtime_snapshot：字段默认值与浮点四舍五入
- get_yesterday_same_period_stats：昨日同时段聚合（今日实时同比对比数据源）
- flush_realtime_metrics：last_flush_at 时间戳写入

说明：全部 Redis 交互均 mock 在源模块层（apps.analytics.services.realtime_service.get_redis_safe），
不依赖真实 Redis；QueueDepthLog 落库用真实 Django 测试库。
"""
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from unittest.mock import patch, MagicMock

from django.utils import timezone

from apps.analytics.services import realtime_service
from apps.analytics.models import QueueDepthLog
from apps.chat.models import QaRecord, Session
from rag_project.config import AnalyticsConfig

QUEUE_NAMES = ['default', 'parse', 'memory', 'email', 'analytics']


def _today_key():
    """生成与实现一致的今日实时指标 Redis key"""
    return f"analytics:realtime:{time.strftime('%Y-%m-%d')}"


# ============================================================================
# _get_redis —— Redis 连接构建（lru_cache 缓存，测试需先清缓存）
# ============================================================================
class TestGetRedis:
    """Redis 连接构建测试"""

    @pytest.mark.unit
    def test_uses_redis_url(self, settings):
        """配置了 REDIS_URL 时解析 host/port/password，DB 切换为 Analytics 专用库"""
        settings.REDIS_URL = 'redis://:secret@rhost:6399/2'
        fake = MagicMock(name='redis_conn')
        realtime_service._get_redis.cache_clear()
        with patch('redis.Redis', return_value=fake) as m:
            r = realtime_service._get_redis()
        assert r is fake
        _, kwargs = m.call_args
        assert kwargs['host'] == 'rhost'
        assert kwargs['port'] == 6399
        assert kwargs['password'] == 'secret'
        assert kwargs['db'] == AnalyticsConfig.redis_db()
        assert kwargs['decode_responses'] is True

    @pytest.mark.unit
    def test_fallback_env_vars(self, settings, monkeypatch):
        """无 REDIS_URL 时从 REDIS_DB_HOST/PORT/PASSWORD 环境变量拼接"""
        settings.REDIS_URL = ''
        monkeypatch.setenv('REDIS_DB_HOST', 'envhost')
        monkeypatch.setenv('REDIS_DB_PORT', '6380')
        monkeypatch.setenv('REDIS_DB_PASSWORD', 'envpass')
        fake = MagicMock(name='redis_conn')
        realtime_service._get_redis.cache_clear()
        with patch('redis.Redis', return_value=fake) as m:
            r = realtime_service._get_redis()
        assert r is fake
        _, kwargs = m.call_args
        assert kwargs['host'] == 'envhost'
        assert kwargs['port'] == 6380
        assert kwargs['password'] == 'envpass'
        assert kwargs['db'] == AnalyticsConfig.redis_db()


# ============================================================================
# get_redis_safe —— 健康检查 TTL 缓存
# ============================================================================
class TestGetRedisSafe:
    """Redis 连接健康检查测试"""

    @pytest.mark.unit
    def test_ping_when_stale(self):
        """超过检查间隔 → ping 一次并更新时间戳"""
        fake = MagicMock(name='redis_conn')
        realtime_service._last_health_check = 0
        with patch('apps.analytics.services.realtime_service._get_redis', return_value=fake) as m:
            r = realtime_service.get_redis_safe()
        assert r is fake
        fake.ping.assert_called_once()
        m.assert_called_once_with()

    @pytest.mark.unit
    def test_no_ping_within_interval(self):
        """间隔内 → 不触发 ping（热点路径零开销）"""
        fake = MagicMock(name='redis_conn')
        realtime_service._last_health_check = time.time()
        with patch('apps.analytics.services.realtime_service._get_redis', return_value=fake):
            realtime_service.get_redis_safe()
        fake.ping.assert_not_called()

    @pytest.mark.unit
    def test_stale_connection_recreated(self):
        """ping 失败 → 清空 lru_cache 并重建连接"""
        fake1 = MagicMock(name='stale')
        fake1.ping.side_effect = Exception('connection lost')
        fake2 = MagicMock(name='fresh')
        with patch('apps.analytics.services.realtime_service._get_redis', side_effect=[fake1, fake2]) as m:
            realtime_service._last_health_check = 0
            r = realtime_service.get_redis_safe()
        assert r is fake2
        assert m.call_count == 2


# ============================================================================
# update_queue_depth —— 队列深度快照（Redis LLEN + PG 落库）
# ============================================================================
@pytest.mark.django_db
class TestUpdateQueueDepth:
    """队列深度更新测试（DB 断言 QueueDepthLog）"""

    def _fake_redis(self, depths):
        """构造假 Redis：pipeline 返回同一对象，execute 返回 LLEN 结果"""
        fake = MagicMock(name='redis_conn')
        pipe = MagicMock(name='pipeline')
        pipe.execute.return_value = depths
        fake.pipeline.return_value = pipe
        return fake

    def _fake_broker(self, depths):
        """构造假 broker Redis：LLEN 由 pipeline.execute 返回（模拟 CELERY_BROKER_URL 连接）"""
        fake = MagicMock(name='broker_conn')
        pipe = MagicMock(name='broker_pipeline')
        pipe.execute.return_value = depths
        fake.pipeline.return_value = pipe
        return fake

    def test_writes_queue_depth_logs(self):
        """5 个队列全部写入 PG，depth 与 broker LLEN 结果一致，minute_bucket 精确到分钟"""
        depths = [3, 0, 2, 1, 5]
        fake_r = self._fake_redis(depths)
        # LLEN 从 broker 连接读取（队列消息所在 Redis DB），写入 current 键走 Analytics DB
        fake_broker = self._fake_broker(depths)
        celery_app = MagicMock()
        celery_app.control.inspect.return_value = None
        with patch('apps.analytics.services.realtime_service.get_redis_safe', return_value=fake_r), \
             patch('apps.analytics.services.realtime_service._get_broker_redis', return_value=fake_broker), \
             patch('rag_project.celery.app', celery_app):
            realtime_service.update_queue_depth()
        logs = list(QueueDepthLog.objects.order_by('queue_name'))
        assert len(logs) == 5
        depth_map = {l.queue_name: l.depth for l in logs}
        assert depth_map == {'default': 3, 'parse': 0, 'memory': 2, 'email': 1, 'analytics': 5}
        assert all(l.worker_count == 0 for l in logs)
        # Celery Redis 传输层队列 key 即队列名本身（如 parse），不带 celery: 前缀；
        # 若误加前缀 LLEN 恒为 0，此处断言防止回归
        llen_calls = [c.args[0] for c in fake_broker.pipeline.return_value.llen.call_args_list]
        assert llen_calls == QUEUE_NAMES
        # minute_bucket 无秒级成分（截断到分钟）
        assert all(l.minute_bucket.second == 0 and l.minute_bucket.microsecond == 0
                   for l in logs)

    def test_worker_count_from_inspect(self):
        """Celery inspect 返回活跃 worker → worker_count 记录到每条日志"""
        depths = [1] * 5
        fake_r = self._fake_redis(depths)
        fake_broker = self._fake_broker(depths)
        celery_app = MagicMock()
        insp = MagicMock()
        insp.active.return_value = {'worker1': ['t1'], 'worker2': ['t2']}
        celery_app.control.inspect.return_value = insp
        with patch('apps.analytics.services.realtime_service.get_redis_safe', return_value=fake_r), \
             patch('apps.analytics.services.realtime_service._get_broker_redis', return_value=fake_broker), \
             patch('rag_project.celery.app', celery_app):
            realtime_service.update_queue_depth()
        assert QueueDepthLog.objects.first().worker_count == 2

    def test_inspect_failure_degrades_worker_zero(self):
        """inspect 抛异常 → worker_count=0，不影响核心指标落库"""
        fake_r = self._fake_redis([0] * 5)
        fake_broker = self._fake_broker([0] * 5)
        celery_app = MagicMock()
        celery_app.control.inspect.side_effect = Exception('broker down')
        with patch('apps.analytics.services.realtime_service.get_redis_safe', return_value=fake_r), \
             patch('apps.analytics.services.realtime_service._get_broker_redis', return_value=fake_broker), \
             patch('rag_project.celery.app', celery_app):
            realtime_service.update_queue_depth()
        assert QueueDepthLog.objects.count() == 5
        assert all(l.worker_count == 0 for l in QueueDepthLog.objects.all())

    def test_bulk_create_conflict_ignored(self):
        """Beat 重入触发唯一约束冲突 → 静默跳过不抛错"""
        fake_r = self._fake_redis([0] * 5)
        fake_broker = self._fake_broker([0] * 5)
        celery_app = MagicMock()
        celery_app.control.inspect.return_value = None
        with patch('apps.analytics.services.realtime_service.get_redis_safe', return_value=fake_r), \
             patch('apps.analytics.services.realtime_service._get_broker_redis', return_value=fake_broker), \
             patch('rag_project.celery.app', celery_app), \
             patch.object(QueueDepthLog.objects, 'bulk_create',
                          side_effect=Exception('duplicate key')):
            realtime_service.update_queue_depth()  # 不应抛异常
        assert QueueDepthLog.objects.count() == 0


# ============================================================================
# get_queue_depth_snapshot —— 队列深度快照（供 API 查询）
# ============================================================================
class TestGetQueueDepthSnapshot:
    """队列快照结构测试"""

    @pytest.mark.unit
    def test_current_key_preferred(self):
        """Redis current 键存在时直接读取，不再 LLEN"""
        fake_r = MagicMock(name='redis_conn')
        fake_r.get.return_value = '7'
        with patch('apps.analytics.services.realtime_service.get_redis_safe', return_value=fake_r), \
             patch('rag_project.celery.app', MagicMock()):
            result = realtime_service.get_queue_depth_snapshot()
        for q in QUEUE_NAMES:
            assert result[q]['size'] == 7
            assert result[q]['length'] == 7
        fake_r.llen.assert_not_called()

    @pytest.mark.unit
    def test_llen_fallback(self):
        """current 键缺失（服务重启）→ 降级直接 LLEN（broker 连接）"""
        fake_r = MagicMock(name='redis_conn')
        fake_r.get.return_value = None
        fake_broker = MagicMock(name='broker_conn')
        fake_broker.llen.return_value = 4
        with patch('apps.analytics.services.realtime_service.get_redis_safe', return_value=fake_r), \
             patch('apps.analytics.services.realtime_service._get_broker_redis', return_value=fake_broker), \
             patch('rag_project.celery.app', MagicMock()):
            result = realtime_service.get_queue_depth_snapshot()
        for q in QUEUE_NAMES:
            assert result[q]['size'] == 4
        # 降级 LLEN 直接查队列名（如 parse），而非 celery:parse（误加前缀恒为 0）
        llen_calls = [c.args[0] for c in fake_broker.llen.call_args_list]
        assert llen_calls == QUEUE_NAMES
        assert fake_broker.llen.call_count == len(QUEUE_NAMES)

    @pytest.mark.unit
    def test_worker_stats_aggregated(self):
        """worker 状态从 Redis 读取（update_queue_depth 任务聚合写入）"""
        fake_r = MagicMock(name='redis_conn')
        fake_r.get.return_value = None
        fake_r.llen.return_value = 0
        pipe = MagicMock(name='pipeline')
        # pipeline.get 按 active/queued/idle 顺序返回，execute 返回同值列表
        pipe.get.side_effect = ['2', '1', '0']
        pipe.execute.return_value = ['2', '1', '0']
        fake_r.pipeline.return_value = pipe
        with patch('apps.analytics.services.realtime_service.get_redis_safe', return_value=fake_r), \
             patch('apps.analytics.services.realtime_service._get_broker_redis', return_value=None):
            result = realtime_service.get_queue_depth_snapshot()
        entry = result['default']
        assert entry['active'] == 2
        assert entry['queued'] == 1
        assert entry['idle'] == 0
        assert entry['failed'] is None

    @pytest.mark.unit
    def test_worker_stats_missing_none(self):
        """Redis 无 worker 状态键（任务未跑/不可用）→ worker 状态全部为 None，队列长度仍正常返回"""
        fake_r = MagicMock(name='redis_conn')
        fake_r.get.return_value = '1'
        pipe = MagicMock(name='pipeline')
        pipe.get.return_value = None
        pipe.execute.return_value = [None, None, None]
        fake_r.pipeline.return_value = pipe
        with patch('apps.analytics.services.realtime_service.get_redis_safe', return_value=fake_r):
            result = realtime_service.get_queue_depth_snapshot()
        entry = result['default']
        assert entry['queued'] is None and entry['active'] is None
        assert entry['idle'] is None and entry['failed'] is None
        assert entry['size'] == 1


# ============================================================================
# increment_realtime_metrics —— 原子递增今日实时指标
# ============================================================================
class TestIncrementRealtimeMetrics:
    """实时指标递增测试（pipeline 各命令断言）"""

    def _qa(self, is_hit_cache=False, is_success=True, tokens_prompt=10,
            tokens_completion=20, cost_estimate='0.5'):
        return SimpleNamespace(
            is_hit_cache=is_hit_cache, is_success=is_success,
            tokens_prompt=tokens_prompt, tokens_completion=tokens_completion,
            cost_estimate=cost_estimate,
        )

    @pytest.mark.unit
    def test_normal_qa_increments_all(self):
        """正常请求：total_qa + normal_qa + token/费用 + TTL，不累加 cache_hits"""
        fake_r = MagicMock(name='redis_conn')
        pipe = fake_r.pipeline.return_value
        with patch('apps.analytics.services.realtime_service.get_redis_safe', return_value=fake_r):
            realtime_service.increment_realtime_metrics(self._qa())
        key = _today_key()
        pipe.hincrby.assert_any_call(key, 'total_qa', 1)
        pipe.hincrby.assert_any_call(key, 'normal_qa', 1)
        pipe.hincrbyfloat.assert_any_call(key, 'tokens_prompt', 10.0)
        pipe.hincrbyfloat.assert_any_call(key, 'tokens_completion', 20.0)
        pipe.hincrbyfloat.assert_any_call(key, 'cost_estimate', 0.5)
        pipe.expire.assert_called_once_with(key, realtime_service.REALTIME_RETENTION_DAYS * 86400)
        # 正常请求不累加 cache_hits
        fields = [c.args[1] for c in pipe.hincrby.call_args_list]
        assert 'cache_hits' not in fields

    @pytest.mark.unit
    def test_cache_hit_only_qa_and_cache_hits(self):
        """缓存命中：只累加 total_qa + cache_hits，不增加 token/费用计数"""
        fake_r = MagicMock(name='redis_conn')
        pipe = fake_r.pipeline.return_value
        with patch('apps.analytics.services.realtime_service.get_redis_safe', return_value=fake_r):
            realtime_service.increment_realtime_metrics(self._qa(is_hit_cache=True))
        key = _today_key()
        pipe.hincrby.assert_any_call(key, 'total_qa', 1)
        pipe.hincrby.assert_any_call(key, 'cache_hits', 1)
        pipe.hincrbyfloat.assert_not_called()

    @pytest.mark.unit
    def test_failure_increments_llm_errors(self):
        """链路失败：额外累加 llm_errors"""
        fake_r = MagicMock(name='redis_conn')
        pipe = fake_r.pipeline.return_value
        with patch('apps.analytics.services.realtime_service.get_redis_safe', return_value=fake_r):
            realtime_service.increment_realtime_metrics(self._qa(is_success=False))
        key = _today_key()
        pipe.hincrby.assert_any_call(key, 'llm_errors', 1)

    @pytest.mark.unit
    def test_none_token_fields_safe(self):
        """tokens/cost 为 None 时安全降级为 0，避免 float(None) TypeError"""
        fake_r = MagicMock(name='redis_conn')
        with patch('apps.analytics.services.realtime_service.get_redis_safe', return_value=fake_r):
            realtime_service.increment_realtime_metrics(self._qa(tokens_prompt=None,
                                                         tokens_completion=None,
                                                         cost_estimate=None))
        pipe = fake_r.pipeline.return_value
        pipe.hincrbyfloat.assert_any_call(_today_key(), 'tokens_prompt', 0.0)


# ============================================================================
# get_realtime_snapshot / flush_realtime_metrics —— 快照读取与时间戳刷新
# ============================================================================
class TestRealtimeSnapshot:
    """今日实时指标快照测试"""

    @pytest.mark.unit
    def test_snapshot_values_and_rounding(self):
        """字段映射完整，浮点字段四舍五入"""
        fake_r = MagicMock(name='redis_conn')
        fake_r.hgetall.return_value = {
            'total_qa': '5', 'cache_hits': '2', 'normal_qa': '3', 'llm_errors': '1',
            'tokens_prompt': '12.34', 'tokens_completion': '8.10',
            'cost_estimate': '0.5', 'last_flush_at': '1700000000',
        }
        with patch('apps.analytics.services.realtime_service.get_redis_safe', return_value=fake_r):
            snap = realtime_service.get_realtime_snapshot()
        assert snap['total_qa'] == 5
        assert snap['cache_hits'] == 2
        assert snap['normal_qa'] == 3
        assert snap['llm_errors'] == 1
        assert snap['tokens_prompt'] == 12.34
        assert snap['tokens_completion'] == 8.1
        assert snap['cost_estimate'] == 0.5
        assert snap['last_flush_at'] == 1700000000
        assert snap['date'] == time.strftime('%Y-%m-%d')

    @pytest.mark.unit
    def test_empty_data_defaults_zero(self):
        """Redis 无数据（hgetall 空）→ 全部字段默认 0"""
        fake_r = MagicMock(name='redis_conn')
        fake_r.hgetall.return_value = {}
        with patch('apps.analytics.services.realtime_service.get_redis_safe', return_value=fake_r):
            snap = realtime_service.get_realtime_snapshot()
        assert snap['total_qa'] == 0 and snap['tokens_prompt'] == 0.0
        assert snap['last_flush_at'] == 0

    @pytest.mark.unit
    def test_flush_writes_timestamp(self):
        """flush_realtime_metrics 写入 last_flush_at 时间戳"""
        fake_r = MagicMock(name='redis_conn')
        with patch('apps.analytics.services.realtime_service.get_redis_safe', return_value=fake_r):
            realtime_service.flush_realtime_metrics()
        key = _today_key()
        assert fake_r.hset.call_count == 1
        args = fake_r.hset.call_args[0]
        assert args[0] == key
        assert args[1] == 'last_flush_at'
        # 时间戳为整数秒
        assert isinstance(args[2], int)


# ============================================================================
# get_yesterday_same_period_stats —— 昨日同时段聚合（今日实时同比对比）
# ============================================================================
@pytest.mark.django_db
class TestYesterdaySamePeriod:
    """昨日同时段聚合测试：窗口与字段口径均与 increment_realtime_metrics 对齐"""

    def _make_qa(self, session, user, created_at, is_hit_cache=False, is_success=True,
                 tokens_prompt=0, tokens_completion=0, cost_estimate=0):
        """创建 QA 记录；created_at 通过 queryset.update 回填（auto_now_add 不可直接赋值）"""
        qa = QaRecord.objects.create(
            session=session, user=user, turn_index=0, question='q', answer='a',
            is_hit_cache=is_hit_cache, is_success=is_success,
            tokens_prompt=tokens_prompt, tokens_completion=tokens_completion,
            cost_estimate=cost_estimate)
        QaRecord.objects.filter(id=qa.id).update(created_at=created_at)
        return qa

    def test_aggregates_only_yesterday_same_period(self, test_user):
        """只统计[昨日0点, 昨日0点+今日已流逝时长)内的记录，缓存/失败/Token/费用口径对齐"""
        session = Session.objects.create(user=test_user, title='t')
        now = timezone.localtime()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed = now - today_start
        y_start = today_start - timedelta(days=1)
        y_end = y_start + elapsed
        # 段内三点按 elapsed 比例取值，避免凌晨 elapsed 很小导致超窗
        y_a = y_start + elapsed * 0.3   # 非缓存成功
        y_b = y_start + elapsed * 0.5   # 缓存命中
        y_c = y_start + elapsed * 0.8   # 非缓存失败
        self._make_qa(session, test_user, y_a, tokens_prompt=100, tokens_completion=50, cost_estimate=0.01)
        self._make_qa(session, test_user, y_b, is_hit_cache=True)
        self._make_qa(session, test_user, y_c, is_success=False, tokens_prompt=20, tokens_completion=10, cost_estimate=0.002)
        # 窗口外：昨日更晚（可能已跨入今日）与今日记录均不计入
        self._make_qa(session, test_user, y_end + timedelta(hours=2))
        self._make_qa(session, test_user, today_start + elapsed * 0.5)

        agg = realtime_service.get_yesterday_same_period_stats()
        assert agg['total_qa'] == 3
        assert agg['cache_hits'] == 1
        assert agg['normal_qa'] == 2
        assert agg['llm_errors'] == 1
        assert agg['tokens_prompt'] == 120
        assert agg['tokens_completion'] == 60
        assert abs(agg['cost_estimate'] - 0.012) < 1e-6

    def test_empty_period_returns_zero(self, test_user):
        """昨日同时段无记录 → 返回全 0 聚合（非 None），前端按持平展示"""
        session = Session.objects.create(user=test_user, title='t')
        now = timezone.localtime()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self._make_qa(session, test_user, today_start + timedelta(minutes=5))

        agg = realtime_service.get_yesterday_same_period_stats()
        assert agg is not None
        assert agg['total_qa'] == 0
        assert agg['cache_hits'] == 0
        assert agg['normal_qa'] == 0
        assert agg['llm_errors'] == 0
        assert agg['cost_estimate'] == 0.0
