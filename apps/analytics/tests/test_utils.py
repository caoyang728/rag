"""
apps.analytics.utils 单元测试 —— 统计辅助函数

覆盖范围：
- calculate_percentile：线性插值百分位（空/单元素/P0/P100/返回 int）
- calculate_percentiles：P50/P95/P99 一次性计算
- build_latency_histogram：延迟分桶（空/桶边界/自定义桶宽/按键排序）
- aggregate_system_metrics（DB）：QaRecord → 系统指标日报聚合
- aggregate_org_usage（DB）：部门/团队双粒度组织使用聚合
- get_queue_depth_history（DB）：队列深度历史查询与字段兼容

说明：
- 纯函数用 pytest.mark.unit（不触 DB），聚合函数用真实 ORM（@pytest.mark.django_db）
- auto_now_add 的 created_at 无法在 create() 时直接写入（pre_save 会覆盖），
  需要跨天数据时用 QuerySet.update() 绕过 pre_save 直接落库
"""
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics import utils
from apps.analytics.models import QueueDepthLog
from apps.chat.models import QaRecord, QaFeedback
from apps.memory.models import Session
from apps.users.models import User, Department, Team


# ============================================================================
# 纯函数：百分位计算
# ============================================================================
class TestCalculatePercentile:
    """calculate_percentile 百分位计算（线性插值法）"""

    @pytest.mark.unit
    def test_empty_returns_zero(self):
        """空列表返回 0（避免上游除零/越界）"""
        assert utils.calculate_percentile([], 50) == 0

    @pytest.mark.unit
    def test_single_element_returns_value(self):
        """单元素返回该元素（int 化）"""
        assert utils.calculate_percentile([7.9], 50) == 7

    @pytest.mark.unit
    def test_linear_interpolation(self):
        """P50 线性插值：[10,20,30,40] → k=1.5 → 20 + 0.5*(30-20) = 25"""
        assert utils.calculate_percentile([10, 20, 30, 40], 50) == 25

    @pytest.mark.unit
    def test_p0_returns_min(self):
        """P0 取最小值（f=0, c=0 分支）"""
        assert utils.calculate_percentile([10, 20, 30, 40], 0) == 10

    @pytest.mark.unit
    def test_p100_returns_max(self):
        """P100 取最大值（f+1 == n 走兜底分支）"""
        assert utils.calculate_percentile([10, 20, 30, 40], 100) == 40

    @pytest.mark.unit
    def test_p95_interpolation(self):
        """P95 线性插值：[10,20,30,40] → k=2.85 → 30 + 0.85*(40-30) = 38.5 → 38"""
        assert utils.calculate_percentile([10, 20, 30, 40], 95) == 38

    @pytest.mark.unit
    def test_unsorted_input(self):
        """输入乱序也能正确计算（内部先排序）"""
        assert utils.calculate_percentile([30, 10, 20], 50) == 20

    @pytest.mark.unit
    def test_returns_int_for_float_values(self):
        """浮点输入返回 int（便于直接存入 IntegerField）"""
        result = utils.calculate_percentile([1.5, 2.5], 50)
        assert result == 2
        assert isinstance(result, int)


class TestCalculatePercentiles:
    """calculate_percentiles 一次性计算 P50/P95/P99"""

    @pytest.mark.unit
    def test_keys(self):
        """返回 p50/p95/p99 三个键"""
        assert set(utils.calculate_percentiles([0, 10, 20, 30])) == {'p50', 'p95', 'p99'}

    @pytest.mark.unit
    def test_values(self):
        """P50=15 / P95=28 / P99=29（线性插值后 int 截断）"""
        assert utils.calculate_percentiles([0, 10, 20, 30]) == {
            'p50': 15, 'p95': 28, 'p99': 29}

    @pytest.mark.unit
    def test_empty_all_zero(self):
        """空列表 → 三个百分位全部为 0"""
        assert utils.calculate_percentiles([]) == {'p50': 0, 'p95': 0, 'p99': 0}


# ============================================================================
# 纯函数：延迟直方图
# ============================================================================
class TestBuildLatencyHistogram:
    """build_latency_histogram 延迟分桶"""

    @pytest.mark.unit
    def test_empty_returns_empty_dict(self):
        """空列表返回 {}（与空查询一致）"""
        assert utils.build_latency_histogram([]) == {}

    @pytest.mark.unit
    def test_basic_bucketing(self):
        """100ms 桶宽：[50,150,250] → 三个桶各 1 次"""
        assert utils.build_latency_histogram([50, 150, 250]) == {
            '0-100': 1, '100-200': 1, '200-300': 1}

    @pytest.mark.unit
    def test_multiple_values_same_bucket(self):
        """同一桶内计数累加"""
        assert utils.build_latency_histogram([50, 60, 150]) == {
            '0-100': 2, '100-200': 1}

    @pytest.mark.unit
    def test_boundary_value(self):
        """桶边界 100 归入 100-200 桶（按 int(v)//bucket_size 向下取整）"""
        assert utils.build_latency_histogram([100]) == {'100-200': 1}

    @pytest.mark.unit
    def test_sorted_by_bucket_start(self):
        """返回结果按键起始值排序（与输入顺序无关）"""
        hist = utils.build_latency_histogram([250, 50, 150])
        assert list(hist.keys()) == ['0-100', '100-200', '200-300']

    @pytest.mark.unit
    def test_custom_bucket_size(self):
        """自定义桶宽 200ms"""
        assert utils.build_latency_histogram([50, 250], bucket_size=200) == {
            '0-200': 1, '200-400': 1}

    @pytest.mark.unit
    def test_float_values(self):
        """浮点延迟值先 int 再分桶"""
        assert utils.build_latency_histogram([50.5]) == {'0-100': 1}


# ============================================================================
# DB 基类：QA 数据准备
# ============================================================================
class UtilsDBTestBase:
    """utils 聚合函数 DB 测试公共基类"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入业务日期/用户/会话"""
        # 本地业务日期：timezone.now().date() 是 UTC 日期，与 __date 查询的
        # 本地时区转换在凌晨时段相差一天，会导致聚合查询匹配不到当天数据
        self.report_date = timezone.localdate()
        self.user = User.objects.create_user(
            username='utils_user', password='pass12345', email='utils@test.com')
        self.session = Session.objects.create(
            user=self.user, root_type='test_root', title='Utils')

    def _create_qa(self, **kwargs):
        """创建 QA 记录（默认归属测试用户/会话，报告日期为今天）"""
        defaults = {
            'session': self.session,
            'user': self.user,
            'question': '测试问题',
            'answer': '测试回答',
            'answer_type': 'rag',
            'root_type': 'test_root',
        }
        defaults.update(kwargs)
        return QaRecord.objects.create(**defaults)

    def _move_created_at(self, record, delta):
        """改写 auto_now_add 的 created_at（update() 绕过 pre_save 覆盖）"""
        QaRecord.objects.filter(pk=record.pk).update(
            created_at=timezone.now() + delta)


@pytest.mark.django_db
class TestAggregateSystemMetrics(UtilsDBTestBase):
    """aggregate_system_metrics 系统指标日报聚合"""

    def test_empty_query_all_zero(self):
        """无数据时所有指标为 0，不抛异常"""
        result = utils.aggregate_system_metrics(report_date=self.report_date)

        assert result['total_qa'] == 0
        assert result['cache_hit_count'] == 0
        assert result['normal_qa_count'] == 0
        assert result['latency_histogram'] == {}
        assert result['error_distribution'] == {}
        assert result['cache_hit_rate'] == 0.0
        assert result['total_cost'] == Decimal('0')

    def test_normal_path(self):
        """正常路径：计数/百分位/Token/成本/错误分布"""
        for i, latency in enumerate([100, 200, 300, 400]):
            self._create_qa(
                question=f'q{i}', latency_total_ms=latency,
                latency_llm_ms=latency // 2, latency_retrieval_ms=50,
                latency_ttfb_ms=100, tokens_prompt=10, tokens_completion=5,
                tokens_per_second=10.0 * (i + 1),
                cost_estimate=Decimal('0.010000'),
                is_success=(i != 3),
                error_type='timeout' if i == 3 else '',
            )
        result = utils.aggregate_system_metrics(report_date=self.report_date)

        assert result['total_qa'] == 4
        assert result['normal_qa_count'] == 4
        assert result['cache_hit_count'] == 0
        # P50=250 / P95=385 / P99=397（线性插值，见 calculate_percentile 测试）
        assert result['p50_latency_total'] == 250
        assert result['p95_latency_total'] == 385
        assert result['p99_latency_total'] == 397
        # 成功率 3/4，超时率 1/4（均只统计非缓存请求）
        assert result['llm_success_rate'] == 0.75
        assert result['llm_timeout_rate'] == 0.25
        # Token 生成速率均值 25.0
        assert result['avg_tokens_per_second'] == 25.0
        assert result['total_tokens_prompt'] == 40
        assert result['total_tokens_completion'] == 20
        assert result['total_cost'] == Decimal('0.040000')
        assert result['error_distribution'] == {'timeout': 1}
        # 延迟直方图（100ms 桶宽）：100→100-200, 200→200-300, 300→300-400, 400→400-500
        assert result['latency_histogram'] == {
            '100-200': 1, '200-300': 1, '300-400': 1, '400-500': 1}

    def test_cache_hit_separated(self):
        """缓存命中延迟与 Token 速率单独统计，不被正常请求稀释"""
        # 2 条正常请求 + 2 条缓存命中（缓存命中 Token 速率设为 999 便于验证过滤）
        self._create_qa(question='n1', latency_total_ms=100, tokens_per_second=10.0)
        self._create_qa(question='n2', latency_total_ms=200, tokens_per_second=20.0)
        self._create_qa(question='c1', latency_total_ms=10,
                        tokens_per_second=999.0, is_hit_cache=True)
        self._create_qa(question='c2', latency_total_ms=20,
                        tokens_per_second=999.0, is_hit_cache=True)

        result = utils.aggregate_system_metrics(report_date=self.report_date)

        assert result['total_qa'] == 4
        assert result['cache_hit_count'] == 2
        assert result['normal_qa_count'] == 2
        assert result['cache_hit_rate'] == 0.5
        # 正常请求 P50 = (100+200)/2 = 150；缓存命中 P50 = (10+20)/2 = 15
        assert result['p50_latency_total'] == 150
        assert result['cache_hit_p50_latency'] == 15
        # avg_tokens_per_second 仅统计非缓存请求：(10+20)/2 = 15
        assert result['avg_tokens_per_second'] == 15.0

    def test_default_values_aggregate(self):
        """未显式设置的延迟/Token 字段按默认 0 参与聚合，不抛异常"""
        self._create_qa(question='defaults')  # 所有 latency/tps 均为默认 0
        self._create_qa(question='with_latency', latency_total_ms=100)

        result = utils.aggregate_system_metrics(report_date=self.report_date)

        assert result['total_qa'] == 2
        # 有效延迟 [0, 100] → P50 = 50；直方图 0→0-100 桶、100→100-200 桶
        assert result['p50_latency_total'] == 50
        assert result['latency_histogram'] == {'0-100': 1, '100-200': 1}

    def test_date_range_filter(self):
        """只统计 report_date 当天的记录，跨天记录不纳入"""
        qa_today = self._create_qa(question='today', latency_total_ms=100)
        qa_old = self._create_qa(question='old', latency_total_ms=200)
        # 把第二条记录改写到 3 天前（update() 绕过 auto_now_add）
        self._move_created_at(qa_old, timedelta(days=-3))

        result = utils.aggregate_system_metrics(report_date=self.report_date)

        assert result['total_qa'] == 1
        assert result['p50_latency_total'] == 100

    def test_default_report_date_is_yesterday(self):
        """不传 report_date 时默认统计昨天"""
        self._create_qa(question='today', latency_total_ms=100)
        qa_yesterday = self._create_qa(question='yesterday', latency_total_ms=200)
        self._move_created_at(qa_yesterday, timedelta(days=-1))

        result = utils.aggregate_system_metrics()

        assert result['total_qa'] == 1
        assert result['p50_latency_total'] == 200


@pytest.mark.django_db
class TestAggregateOrgUsage:
    """aggregate_org_usage 部门/团队双粒度组织使用聚合"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入部门/团队/用户/会话"""
        # 同 UtilsDBTestBase：report_date 用本地业务日期，与 __date 查询时区转换对齐
        self.report_date = timezone.localdate()
        self.dept = Department.objects.create(name='研发部')
        self.team = Team.objects.create(name='平台组', department=self.dept)
        self.user1 = User.objects.create_user(
            username='org_u1', password='pass12345', email='org_u1@test.com',
            department=self.dept, team=self.team)
        self.user2 = User.objects.create_user(
            username='org_u2', password='pass12345', email='org_u2@test.com',
            department=self.dept, team=self.team)
        self.session1 = Session.objects.create(user=self.user1, root_type='test_root')
        self.session2 = Session.objects.create(user=self.user2, root_type='test_root')

    def _create_qa(self, user, session, latency=200, is_hit_cache=False):
        """创建组织聚合用的 QA 记录（tokens/cost 固定便于断言）"""
        return QaRecord.objects.create(
            session=session, user=user, question='测试问题', answer='回答',
            answer_type='rag', root_type='test_root',
            latency_total_ms=latency, tokens_prompt=100, tokens_completion=50,
            cost_estimate=Decimal('0.010000'), is_hit_cache=is_hit_cache)

    def test_dept_and_team_rows(self):
        """同时生成部门级（team_id=-1）与团队级两条明细，指标一致"""
        qa1 = self._create_qa(self.user1, self.session1, latency=200)
        qa2 = self._create_qa(self.user2, self.session2, latency=400,
                              is_hit_cache=True)
        QaFeedback.objects.create(qa_record=qa1, user=self.user1, rating=1)
        QaFeedback.objects.create(qa_record=qa2, user=self.user2, rating=-1)

        results = utils.aggregate_org_usage(report_date=self.report_date)
        assert len(results) == 2

        by_key = {(r['department_id'], r['team_id']): r for r in results}
        dept_row = by_key[(self.dept.id, -1)]
        team_row = by_key[(self.dept.id, self.team.id)]

        for row in (dept_row, team_row):
            assert row['qa_count'] == 2
            assert row['user_count'] == 2
            assert row['total_tokens'] == 300
            assert row['total_cost'] == Decimal('0.020000')
            assert row['avg_latency_ms'] == 300
            assert row['p95_latency_ms'] == 390
            assert row['good_feedback_rate'] == 0.5
            assert row['cache_hit_count'] == 1
            assert row['cache_hit_rate'] == 0.5

        # 部门级 team_name 为空，团队级携带团队名
        assert dept_row['team_name'] == ''
        assert team_row['team_name'] == '平台组'

    def test_user_without_department_skipped(self):
        """无部门归属的用户不产生任何组织行"""
        user_no_dept = User.objects.create_user(
            username='org_none', password='pass12345', email='org_none@test.com')
        session = Session.objects.create(user=user_no_dept, root_type='test_root')
        self._create_qa(user_no_dept, session)

        assert utils.aggregate_org_usage(report_date=self.report_date) == []

    def test_dept_user_without_team_only_dept_row(self):
        """有部门无团队 → 只生成部门级汇总行"""
        user = User.objects.create_user(
            username='org_dept_only', password='pass12345',
            email='org_dept_only@test.com', department=self.dept)
        session = Session.objects.create(user=user, root_type='test_root')
        self._create_qa(user, session)

        results = utils.aggregate_org_usage(report_date=self.report_date)
        assert len(results) == 1
        assert results[0]['team_id'] == -1
        assert results[0]['qa_count'] == 1
        assert results[0]['user_count'] == 1

    def test_anonymous_qa_skipped(self):
        """匿名（user=None）QA 不纳入组织聚合"""
        self._create_qa(None, self.session1)

        assert utils.aggregate_org_usage(report_date=self.report_date) == []


@pytest.mark.django_db
class TestGetQueueDepthHistory:
    """get_queue_depth_history 队列深度历史查询"""

    def test_within_window_returns_logs(self):
        """窗口内日志全部返回，新老字段兼容（queued_size 回退 depth）"""
        now = timezone.now()
        QueueDepthLog.objects.create(
            queue_name='default', depth=5, worker_count=2,
            minute_bucket=now.replace(second=0, microsecond=0))
        QueueDepthLog.objects.create(
            queue_name='parse', depth=3, worker_count=1)

        result = utils.get_queue_depth_history(hours=24)
        assert len(result) == 2

        by_name = {r['queue_name']: r for r in result}

        # 有 minute_bucket：转本地时间并格式化为 YYYYMMDDHHmm（12 位数字），
        # 前端 slice(8,10):slice(10,12) 即得 HH:MM；不能用 str() 直出 ISO 字符串
        r1 = by_name['default']
        assert re.search(r'^\d{12}$', r1['minute_bucket'])
        assert r1['minute_bucket'] == timezone.localtime(
            QueueDepthLog.objects.get(queue_name='default').minute_bucket
        ).strftime('%Y%m%d%H%M')
        assert r1['queued_size'] == 5   # 模型无该字段 → 回退 depth
        assert r1['active_size'] == 0   # 模型无该字段 → 0
        assert r1['failed_count'] == 0  # 模型无该字段 → 0
        assert r1['depth'] == 5
        assert r1['worker_count'] == 2

        # 无 minute_bucket：从 created_at 构造 YYYYMMDDHHmm（12 位数字）
        r2 = by_name['parse']
        assert re.search(r'^\d{12}$', r2['minute_bucket'])
        assert r2['queued_size'] == 3

    def test_outside_window_excluded(self):
        """窗口外的历史日志被过滤"""
        log = QueueDepthLog.objects.create(
            queue_name='default', depth=1, worker_count=1)
        # 改写到 25 小时前，超出 24h 窗口
        QueueDepthLog.objects.filter(pk=log.pk).update(
            created_at=timezone.now() - timedelta(hours=25))

        assert utils.get_queue_depth_history(hours=24) == []

    def test_ordering_by_created_at(self):
        """按 created_at 升序返回（先创建的在前）"""
        older = QueueDepthLog.objects.create(
            queue_name='parse', depth=1, worker_count=1)
        newer = QueueDepthLog.objects.create(
            queue_name='default', depth=2, worker_count=1)
        # 强制改写 created_at，保证排序可控
        QueueDepthLog.objects.filter(pk=older.pk).update(
            created_at=timezone.now() - timedelta(minutes=5))

        result = utils.get_queue_depth_history(hours=24)
        assert [r['queue_name'] for r in result] == ['parse', 'default']


@pytest.mark.django_db
class TestAggregateRouteAnalysis(UtilsDBTestBase):
    """aggregate_route_analysis 路由决策分析按日聚合"""

    def _create_routed_qa(self, route_source, trace=None, latency_ms=100, **kw):
        """创建带路由来源的 QA 记录（默认单层 route_trace）"""
        defaults = dict(
            route_source=route_source,
            route_trace=trace or [
                {'layer': route_source, 'confidence': 0.8, 'latency_ms': 50}],
            latency_total_ms=latency_ms,
        )
        defaults.update(kw)
        return self._create_qa(**defaults)

    def test_aggregates_route_row(self):
        """正常路径：QA → RouteAnalysis，携带 source/置信度/延迟/质量分/提问时间"""
        from apps.analytics.models import RouteAnalysis, MultiDimensionScore
        qa = self._create_routed_qa('wiki', latency_ms=200)
        # 该 QA 已有 12 维评估（仅 1 维，均分=0.9）
        MultiDimensionScore.objects.create(
            qa_record=qa, dimension='faithfulness', score=0.9,
            reason='r', eval_model='deepseek-chat', status='completed')

        result = utils.aggregate_route_analysis(report_date=self.report_date)

        assert result['total'] == 1
        assert result['created'] == 1
        route = RouteAnalysis.objects.get(qa_record_id=qa.id)
        assert route.route_source == 'wiki'
        assert route.confidence == 0.8
        assert route.latency_ms == 200
        assert route.answer_quality == 0.9
        assert route.qa_created_at == qa.created_at

    def test_idempotent(self):
        """重复执行 → 覆盖而非新增（qa_record_id 唯一，聚合任务幂等）"""
        from apps.analytics.models import RouteAnalysis
        self._create_routed_qa('rag')

        first = utils.aggregate_route_analysis(report_date=self.report_date)
        second = utils.aggregate_route_analysis(report_date=self.report_date)

        assert first['created'] == 1 and first['updated'] == 0
        assert second['created'] == 0 and second['updated'] == 1
        assert RouteAnalysis.objects.count() == 1

    def test_confidence_takes_last_trace_layer(self):
        """confidence 取 route_trace 最后一层（路由按序尝试、命中即返回，末层即胜出层）"""
        from apps.analytics.models import RouteAnalysis
        trace = [
            {'layer': 'wiki', 'confidence': 0.9, 'latency_ms': 10},
            {'layer': 'rag', 'confidence': 0.3, 'latency_ms': 20},
        ]
        self._create_routed_qa('rag', trace=trace)

        utils.aggregate_route_analysis(report_date=self.report_date)

        route = RouteAnalysis.objects.get(route_source='rag')
        assert route.confidence == 0.3

    def test_excludes_qa_without_route(self):
        """无路由来源(route_source 为空)的 QA 不进入路由分析表"""
        self._create_qa(route_source='')  # 普通 RAG 问答，无路由决策
        result = utils.aggregate_route_analysis(report_date=self.report_date)
        assert result['total'] == 0

    def test_default_report_date_is_yesterday(self):
        """未传 report_date 时按昨天聚合（今天数据不落库）"""
        from apps.analytics.models import RouteAnalysis
        self._create_routed_qa('wiki')  # created_at = 今天
        utils.aggregate_route_analysis()
        assert RouteAnalysis.objects.count() == 0
