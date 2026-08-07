"""
apps.analytics.tasks 单元测试 —— 13 个 Celery 定时任务

覆盖范围：
- compute_system_metrics_daily：聚合创建 / 重复执行 update_or_create / 聚合异常
- compute_org_usage_daily：无数据 / 新建 / 更新 / 异常
- update_queue_depth_snapshot：开关关闭跳过 / 成功 / 异常
- flush_realtime_metrics_task：成功 / 异常
- aggregate_daily_report：废弃 no-op 兼容
- cleanup_old_data：过期数据删除（90 天队列日志 / 365 天组织报表），新数据保留
- batch_evaluate_document_quality / generate_coverage_report_daily：成功 / 异常
- run_multi_dimension_evaluation：开关 / 预算拦截 / 预算异常继续 / 无待评估 / 成功落库 / 单条失败继续
- run_low_score_analysis：开关 / QA 不存在 / 无评分 / 均分达标跳过 / 归因落库 / 归因失败落 failed
- periodic_retrieval_evaluation：无测试集 / 无用户 / 成功 / 单测试集失败继续
- siphon_low_score_regression / run_regression_evaluation_task：开关 / 成功 / 异常

说明：聚合函数与外部评估引擎全部在源模块导入处 mock
（apps.analytics.utils / doc_quality / coverage / offline_eval / deepeval_metrics /
production_eval / regression_eval），DB 落库用真实 Django 测试库。
"""
import pytest
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from django.utils import timezone

from apps.analytics import tasks
from apps.analytics.models import (
    SystemMetricsReport, OrgUsageReport, QueueDepthLog,
    GoldenDataset, GoldenQuestion, MultiDimensionScore, LowScoreAnalysis,
)
from apps.chat.models import QaRecord
from apps.memory.models import Session
from apps.users.models import User
from rag_project.config import AnalyticsConfig


def _yesterday() -> "date":
    """聚合任务使用的报告日期 = 昨天（本地业务日期，与 tasks.py 的 timezone.localdate() 一致）"""
    return timezone.localdate() - timedelta(days=1)


# ============================================================================
# 1. 每日系统指标聚合
# ============================================================================
@pytest.mark.django_db
class TestComputeSystemMetricsDaily:
    """compute_system_metrics_daily 测试"""

    @pytest.mark.unit
    def test_creates_report(self):
        """聚合成功 → 创建 SystemMetricsReport 并返回 created=True"""
        with patch('apps.analytics.utils.aggregate_system_metrics',
                   return_value={'total_qa': 3, 'cache_hit_count': 1, 'normal_qa_count': 2}):
            result = tasks.compute_system_metrics_daily()
        assert result['ok'] is True
        assert result['created'] is True
        assert result['report_date'] == str(_yesterday())
        report = SystemMetricsReport.objects.get(report_date=_yesterday())
        assert report.total_qa == 3

    @pytest.mark.unit
    def test_update_existing_report(self):
        """重复执行 → update_or_create 更新已有报告（UPSERT 幂等）"""
        SystemMetricsReport.objects.create(report_date=_yesterday(), total_qa=1)
        with patch('apps.analytics.utils.aggregate_system_metrics',
                   return_value={'total_qa': 9, 'cache_hit_count': 0, 'normal_qa_count': 9}):
            result = tasks.compute_system_metrics_daily()
        assert result['created'] is False
        assert SystemMetricsReport.objects.filter(report_date=_yesterday()).count() == 1
        assert SystemMetricsReport.objects.get(report_date=_yesterday()).total_qa == 9

    @pytest.mark.unit
    def test_aggregation_exception(self):
        """聚合抛异常 → 返回 {'ok': False}，不中断其他任务"""
        with patch('apps.analytics.utils.aggregate_system_metrics',
                   side_effect=RuntimeError('db down')):
            result = tasks.compute_system_metrics_daily()
        assert result == {'ok': False, 'error': 'aggregation_failed'}


# ============================================================================
# 2. 每日组织使用数据聚合
# ============================================================================
@pytest.mark.django_db
class TestComputeOrgUsageDaily:
    """compute_org_usage_daily 测试"""

    def _agg_data(self, qa_count=3):
        """构造一条 OrgUsageReport 兼容的聚合结果（含 report_date 字段）"""
        return {
            'report_date': _yesterday(),
            'department_id': 1,
            'department_name': '研发部',
            'team_id': -1,
            'team_name': '',
            'qa_count': qa_count,
            'user_count': 2,
            'total_tokens': 100,
            'total_cost': Decimal('1.5'),
            'avg_latency_ms': 10,
            'p95_latency_ms': 20,
            'good_feedback_rate': 0.5,
            'cache_hit_count': 0,
            'cache_hit_rate': 0.0,
        }

    @pytest.mark.unit
    def test_no_data(self):
        """聚合结果为空 → 直接返回不写库"""
        with patch('apps.analytics.utils.aggregate_org_usage', return_value=[]):
            result = tasks.compute_org_usage_daily()
        assert result == {'ok': True, 'date': str(_yesterday()), 'created': 0, 'updated': 0}
        assert OrgUsageReport.objects.count() == 0

    @pytest.mark.unit
    def test_create_new(self):
        """有聚合结果且库中无记录 → bulk_create 新建"""
        with patch('apps.analytics.utils.aggregate_org_usage',
                   return_value=[self._agg_data()]):
            result = tasks.compute_org_usage_daily()
        assert result['ok'] is True
        assert result['created'] == 1
        report = OrgUsageReport.objects.get(report_date=_yesterday())
        assert report.department_name == '研发部'
        assert report.qa_count == 3

    @pytest.mark.unit
    def test_update_existing(self):
        """已存在同 (date, dept, team) 记录 → bulk_update 更新"""
        OrgUsageReport.objects.create(
            report_date=_yesterday(), department_id=1, team_id=-1, qa_count=1)
        with patch('apps.analytics.utils.aggregate_org_usage',
                   return_value=[self._agg_data(qa_count=8)]):
            result = tasks.compute_org_usage_daily()
        assert result['updated'] == 1
        assert OrgUsageReport.objects.count() == 1
        assert OrgUsageReport.objects.get(report_date=_yesterday()).qa_count == 8

    @pytest.mark.unit
    def test_exception(self):
        """聚合异常 → 返回 {'ok': False}"""
        with patch('apps.analytics.utils.aggregate_org_usage',
                   side_effect=RuntimeError('boom')):
            result = tasks.compute_org_usage_daily()
        assert result == {'ok': False, 'error': 'aggregation_failed'}


# ============================================================================
# 3. 队列深度快照 & 4. 实时指标刷新
# ============================================================================
class TestQueueAndRealtimeTasks:
    """队列监控 / 实时指标定时任务测试（全部 mock Redis 交互）"""

    @pytest.mark.unit
    def test_queue_disabled_skips(self):
        """队列监控开关关闭 → 直接跳过"""
        with patch.object(AnalyticsConfig, 'queue_monitor_enabled', return_value=False), \
             patch('apps.analytics.realtime.update_queue_depth') as mock_fn:
            result = tasks.update_queue_depth_snapshot()
        assert result == {'ok': True, 'skipped': True}
        mock_fn.assert_not_called()

    @pytest.mark.unit
    def test_queue_success(self):
        """开启 → 调用 realtime.update_queue_depth"""
        with patch.object(AnalyticsConfig, 'queue_monitor_enabled', return_value=True), \
             patch('apps.analytics.realtime.update_queue_depth') as mock_fn:
            result = tasks.update_queue_depth_snapshot()
        assert result == {'ok': True}
        mock_fn.assert_called_once_with()

    @pytest.mark.unit
    def test_queue_exception(self):
        """更新失败 → 返回 {'ok': False, 'error': 'update_failed'}"""
        with patch.object(AnalyticsConfig, 'queue_monitor_enabled', return_value=True), \
             patch('apps.analytics.realtime.update_queue_depth',
                   side_effect=RuntimeError('redis down')):
            result = tasks.update_queue_depth_snapshot()
        assert result == {'ok': False, 'error': 'update_failed'}

    @pytest.mark.unit
    def test_flush_success(self):
        """实时指标刷新成功"""
        with patch('apps.analytics.realtime.flush_realtime_metrics') as mock_fn:
            result = tasks.flush_realtime_metrics_task()
        assert result == {'ok': True}
        mock_fn.assert_called_once_with()

    @pytest.mark.unit
    def test_flush_exception(self):
        """刷新异常 → 返回 {'ok': False, 'error': 'flush_failed'}"""
        with patch('apps.analytics.realtime.flush_realtime_metrics',
                   side_effect=RuntimeError('redis down')):
            result = tasks.flush_realtime_metrics_task()
        assert result == {'ok': False, 'error': 'flush_failed'}


# ============================================================================
# 5. 每日报表聚合入口（废弃 no-op 兼容）
# ============================================================================
class TestAggregateDailyReport:
    """aggregate_daily_report 兼容测试"""

    @pytest.mark.unit
    def test_deprecated_noop(self):
        """已拆分为独立任务，本函数仅保留兼容返回"""
        assert tasks.aggregate_daily_report() == {
            'ok': True, 'skipped': True, 'reason': 'deprecated_noop'}


# ============================================================================
# 7. 数据清理任务
# ============================================================================
@pytest.mark.django_db
class TestCleanupOldData:
    """cleanup_old_data 保留策略测试"""

    def test_deletes_expired_keeps_recent(self):
        """删除 90 天前队列日志 + 365 天前组织报表，保留新数据"""
        now = timezone.now()
        old_log = QueueDepthLog.objects.create(queue_name='default', depth=5)
        new_log = QueueDepthLog.objects.create(queue_name='parse', depth=2)
        # created_at 是 auto_now_add，需用 update() 回拨模拟历史数据
        QueueDepthLog.objects.filter(id=old_log.id).update(
            created_at=now - timedelta(days=91))
        QueueDepthLog.objects.filter(id=new_log.id).update(
            created_at=now - timedelta(days=10))

        OrgUsageReport.objects.create(
            report_date=(now - timedelta(days=400)).date(), department_id=1, team_id=-1)
        OrgUsageReport.objects.create(
            report_date=(now - timedelta(days=30)).date(), department_id=2, team_id=-1)

        result = tasks.cleanup_old_data()
        assert result['queue_depth_logs_deleted'] == 1
        assert result['org_usage_reports_deleted'] == 1
        # 新数据保留
        assert QueueDepthLog.objects.filter(id=new_log.id).exists()
        assert OrgUsageReport.objects.filter(department_id=2).exists()
        assert not QueueDepthLog.objects.filter(id=old_log.id).exists()

    def test_nothing_to_clean(self):
        """全部数据都在保留期内 → 删除 0 条"""
        QueueDepthLog.objects.create(queue_name='default', depth=1)
        result = tasks.cleanup_old_data()
        assert result['queue_depth_logs_deleted'] == 0
        assert result['org_usage_reports_deleted'] == 0


# ============================================================================
# 8. 文档质量批量评估 & 9. 覆盖率报告生成
# ============================================================================
class TestBatchEvalAndCoverage:
    """文档质量 / 覆盖率定时任务测试"""

    @pytest.mark.unit
    def test_doc_quality_success(self):
        """批量文档质量评估成功 → 返回汇总"""
        summary = {'total_documents': 2, 'evaluated': 2, 'failed': 0}
        with patch('apps.analytics.doc_quality.batch_evaluate_document_quality',
                   return_value=summary) as mock_fn:
            result = tasks.batch_evaluate_document_quality(days=7)
        assert result == {'ok': True, 'summary': summary}
        mock_fn.assert_called_once_with(days=7)

    @pytest.mark.unit
    def test_doc_quality_exception(self):
        """文档质量评估异常 → {'ok': False, 'error': 'batch_eval_failed'}"""
        with patch('apps.analytics.doc_quality.batch_evaluate_document_quality',
                   side_effect=RuntimeError('x')):
            result = tasks.batch_evaluate_document_quality()
        assert result == {'ok': False, 'error': 'batch_eval_failed'}

    @pytest.mark.unit
    def test_coverage_success(self):
        """覆盖率报告生成成功 → 返回报告摘要"""
        report = SimpleNamespace(
            id=1, report_date=_yesterday(),
            hot_query_coverage_rate=0.8, gap_count=2)
        with patch('apps.analytics.coverage.generate_coverage_report',
                   return_value=report) as mock_fn:
            result = tasks.generate_coverage_report_daily(days=7)
        assert result == {'ok': True, 'report_id': 1,
                          'coverage_rate': 0.8, 'gap_count': 2}
        mock_fn.assert_called_once_with(days=7)

    @pytest.mark.unit
    def test_coverage_exception(self):
        """覆盖率报告异常 → {'ok': False, 'error': 'coverage_report_failed'}"""
        with patch('apps.analytics.coverage.generate_coverage_report',
                   side_effect=RuntimeError('x')):
            result = tasks.generate_coverage_report_daily()
        assert result == {'ok': False, 'error': 'coverage_report_failed'}


# ============================================================================
# 10. 多维度回答质量批量评估（每 2 小时）
# ============================================================================
@pytest.mark.django_db
class TestRunMultiDimensionEvaluation:
    """run_multi_dimension_evaluation 批量回扫测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户与会话"""
        self.user = User.objects.create_user(
            username='mde_user', password='pass12345', email='mde@test.com')
        self.session = Session.objects.create(
            user=self.user, root_type='test_root', title='MDE')

    def _qa(self, question='问题', **kw):
        """创建一条成功且非拒答的 QA 记录（默认在最近 2h 窗口内）"""
        defaults = dict(session=self.session, user=self.user, question=question,
                        answer='回答', root_type='test_root', is_success=True)
        defaults.update(kw)
        return QaRecord.objects.create(**defaults)

    def _budget_mocks(self):
        """批量回扫所需的预算与评估 mock（按序展开）"""
        return [
            patch.object(AnalyticsConfig, 'eval_enabled', return_value=True),
            patch('apps.analytics.production_eval._get_redis',
                  return_value=MagicMock()),
            patch('apps.analytics.production_eval._check_daily_budget',
                  return_value=(True, '')),
            patch.object(AnalyticsConfig, 'production_eval_batch_size', return_value=10),
            patch.object(AnalyticsConfig, 'eval_model', return_value='test-model'),
            patch('apps.analytics.production_eval._build_context_list',
                  return_value=['上下文']),
        ]

    def test_disabled(self):
        """评估总开关关闭 → 直接跳过"""
        with patch.object(AnalyticsConfig, 'eval_enabled', return_value=False):
            result = tasks.run_multi_dimension_evaluation()
        assert result == {'ok': True, 'skipped': True, 'reason': 'disabled'}

    def test_budget_blocked(self):
        """日预算超限 → 一次性拦截不评估"""
        mocks = self._budget_mocks()
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5]:
            with patch('apps.analytics.production_eval._check_daily_budget',
                       return_value=(False, 'cost_limit_exceeded')):
                result = tasks.run_multi_dimension_evaluation()
        assert result['reason'] == 'cost_limit_exceeded'
        assert result['skipped'] is True

    def test_budget_error_continues(self):
        """预算检查异常 → 记录警告继续评估（不阻塞批量任务）"""
        self._qa(question='q1')
        mocks = self._budget_mocks()
        eval_result = [{'dimension': 'clarity', 'score': 0.9, 'reason': '清晰',
                        'latency_ms': 3}]
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], \
             patch('apps.analytics.production_eval._check_daily_budget',
                   side_effect=RuntimeError('redis down')), \
             patch('apps.analytics.deepeval_metrics.evaluate_with_deepeval',
                   return_value=eval_result):
            result = tasks.run_multi_dimension_evaluation()
        assert result['ok'] is True
        assert result['evaluated'] == 1

    def test_no_pending_qa(self):
        """窗口内无未评估 QA → evaluated=0"""
        mocks = self._budget_mocks()
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5]:
            result = tasks.run_multi_dimension_evaluation()
        assert result == {'ok': True, 'evaluated': 0}

    def test_success_persists_scores(self):
        """评估成功 → 逐维度落 MultiDimensionScore（batch_id 标记）"""
        self._qa(question='成功问题')
        mocks = self._budget_mocks()
        eval_result = [{'dimension': 'clarity', 'score': 0.9, 'reason': '清晰',
                        'latency_ms': 3}]
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], \
             patch('apps.analytics.deepeval_metrics.evaluate_with_deepeval',
                   return_value=eval_result):
            result = tasks.run_multi_dimension_evaluation()
        assert result == {'ok': True, 'evaluated': 1}
        score = MultiDimensionScore.objects.get(dimension='clarity')
        assert score.score == 0.9
        assert score.eval_model == 'deepeval-test-model'
        assert score.status == 'completed'
        assert score.eval_batch_id.startswith('batch_')

    def test_single_failure_continues(self):
        """单条 QA 评估失败 → 跳过继续评估其余"""
        qa1 = self._qa(question='失败问题')
        qa2 = self._qa(question='成功问题2')
        mocks = self._budget_mocks()
        ok_result = [{'dimension': 'clarity', 'score': 0.8, 'reason': 'ok'}]
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], \
             patch('apps.analytics.deepeval_metrics.evaluate_with_deepeval',
                   side_effect=[RuntimeError('llm down'), ok_result]):
            result = tasks.run_multi_dimension_evaluation()
        assert result['evaluated'] == 1
        assert not MultiDimensionScore.objects.filter(qa_record=qa1).exists()
        assert MultiDimensionScore.objects.filter(qa_record=qa2).exists()

    def test_already_evaluated_excluded(self):
        """24h 内已评估的 QA 不重复评估"""
        qa = self._qa(question='已评估')
        MultiDimensionScore.objects.create(
            qa_record=qa, dimension='clarity', score=0.9, status='completed')
        mocks = self._budget_mocks()
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], \
             patch('apps.analytics.deepeval_metrics.evaluate_with_deepeval') as mock_eval:
            result = tasks.run_multi_dimension_evaluation()
        assert result['evaluated'] == 0
        mock_eval.assert_not_called()

    def test_refused_qa_excluded(self):
        """answer_type='refused' 的拒答不参与批量评估"""
        self._qa(question='拒答', answer_type='refused')
        mocks = self._budget_mocks()
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], \
             patch('apps.analytics.deepeval_metrics.evaluate_with_deepeval') as mock_eval:
            result = tasks.run_multi_dimension_evaluation()
        assert result['evaluated'] == 0
        mock_eval.assert_not_called()


# ============================================================================
# 11. 低分对话归因分析
# ============================================================================
@pytest.mark.django_db
class TestRunLowScoreAnalysis:
    """run_low_score_analysis 归因任务测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入用户/会话/QA 记录"""
        self.user = User.objects.create_user(
            username='lsa_user', password='pass12345', email='lsa@test.com')
        self.session = Session.objects.create(
            user=self.user, root_type='test_root', title='LSA')
        self.qa = QaRecord.objects.create(
            session=self.session, user=self.user, question='低分问题',
            answer='回答', root_type='test_root')

    def _add_scores(self, score=0.3):
        """给 QA 添加一条指定分数的评估记录"""
        return MultiDimensionScore.objects.create(
            qa_record=self.qa, dimension='clarity', score=score,
            status='completed')

    def _analyze_result(self):
        """analyze_low_score_qa 的标准返回结构"""
        return {
            'avg_score': 0.3,
            'category': 'content_gap',
            'detail': '知识盲区：缺少相关文档',
            'affected_layer': 'content',
            'low_dimensions': [{'dimension': 'clarity', 'score': 0.3, 'reason': 'r'}],
            'diagnosis': '检索无结果导致回答质量低',
            'suggestions': [{'type': 'short_term', 'action': '补充文档'}],
            'method': 'rule',
            'model': '',
            'tokens': 0,
            'cost': 0.0,
            'latency_ms': 1,
        }

    def test_disabled(self):
        """评估开关关闭 → 跳过归因"""
        with patch.object(AnalyticsConfig, 'eval_enabled', return_value=False):
            result = tasks.run_low_score_analysis(self.qa.id)
        assert result == {'ok': True, 'skipped': True, 'reason': 'disabled'}

    def test_qa_not_found(self):
        """QA 不存在 → qa_not_found"""
        with patch.object(AnalyticsConfig, 'eval_enabled', return_value=True):
            result = tasks.run_low_score_analysis(999999)
        assert result == {'ok': False, 'reason': 'qa_not_found'}

    def test_no_scores(self):
        """无评估记录 → no_scores"""
        with patch.object(AnalyticsConfig, 'eval_enabled', return_value=True):
            result = tasks.run_low_score_analysis(self.qa.id)
        assert result == {'ok': False, 'reason': 'no_scores'}

    def test_score_above_threshold_skipped(self):
        """均分达标（>= 阈值 0.5）→ 不归因"""
        self._add_scores(score=0.9)
        with patch.object(AnalyticsConfig, 'eval_enabled', return_value=True), \
             patch('apps.analytics.low_score_analyzer.analyze_low_score_qa') as mock_ana:
            result = tasks.run_low_score_analysis(self.qa.id)
        assert result['ok'] is True
        assert result['skipped'] is True
        assert result['reason'] == 'score_above_threshold'
        mock_ana.assert_not_called()

    def test_success_persists(self):
        """归因成功 → LowScoreAnalysis 落库（update_or_create 覆盖）"""
        self._add_scores(score=0.3)
        with patch.object(AnalyticsConfig, 'eval_enabled', return_value=True), \
             patch('apps.analytics.low_score_analyzer.analyze_low_score_qa',
                   return_value=self._analyze_result()):
            result = tasks.run_low_score_analysis(self.qa.id)
        assert result['ok'] is True
        assert result['category'] == 'content_gap'
        analysis = LowScoreAnalysis.objects.get(qa_record=self.qa)
        assert analysis.status == 'completed'
        assert analysis.avg_score == 0.3
        assert analysis.root_cause_category == 'content_gap'
        assert analysis.analysis_method == 'rule'
        # 再次执行 → 覆盖旧记录不重复
        tasks.run_low_score_analysis(self.qa.id)
        assert LowScoreAnalysis.objects.filter(qa_record=self.qa).count() == 1

    def test_failure_persists_failed(self):
        """归因异常 → 落一条 status=failed 记录并返回失败原因"""
        self._add_scores(score=0.3)
        with patch.object(AnalyticsConfig, 'eval_enabled', return_value=True), \
             patch('apps.analytics.low_score_analyzer.analyze_low_score_qa',
                   side_effect=RuntimeError('llm down')):
            result = tasks.run_low_score_analysis(self.qa.id)
        assert result['ok'] is False
        assert result['reason'].startswith('analysis_failed:')
        analysis = LowScoreAnalysis.objects.get(qa_record=self.qa)
        assert analysis.status == 'failed'
        assert 'llm down' in analysis.error_message


# ============================================================================
# 12. 周期性离线检索评估（每周）
# ============================================================================
@pytest.mark.django_db
class TestPeriodicRetrievalEvaluation:
    """periodic_retrieval_evaluation 测试"""

    def _dataset(self, name='黄金测试集'):
        """创建活跃且有问题的测试集"""
        return GoldenDataset.objects.create(
            name=name, status='active', question_count=2)

    def _report(self, recall=0.8, mrr=0.5):
        """构造检索评估报告（SimpleNamespace 模拟 RetrievalQualityReport）"""
        return SimpleNamespace(recall_at_10=recall, mrr=mrr)

    def test_no_datasets_skipped(self):
        """无活跃测试集 → 直接跳过"""
        result = tasks.periodic_retrieval_evaluation()
        assert result == {'ok': True, 'skipped': True}

    def test_no_user_error(self):
        """有测试集但无 system/超管用户 → 返回错误

        项目 User 模型无 is_superuser 字段,源码 filter(is_superuser=True) 在真实
        模型上会抛 FieldError;此处 mock User 模型,两次 filter 均返回 None。
        """
        self._dataset()
        mock_user = MagicMock(name='User')
        mock_user.objects.filter.return_value.first.side_effect = [None, None]
        with patch('apps.users.models.User', mock_user):
            result = tasks.periodic_retrieval_evaluation()
        assert result == {'ok': False, 'error': 'no_user_for_eval'}

    def test_success(self):
        """有测试集 + 系统用户 → 逐个测试集评估并汇总 Recall/MRR"""
        ds = self._dataset(name='HR')
        User.objects.create_user(
            username='system', password='pass12345', email='sys@test.com')
        with patch('apps.analytics.offline_eval.run_retrieval_evaluation',
                   return_value=self._report()) as mock_eval:
            result = tasks.periodic_retrieval_evaluation()
        assert result['ok'] is True
        assert result['evaluated_datasets'] == 1
        assert result['results'][0] == {'dataset': 'HR', 'recall_at_10': 0.8, 'mrr': 0.5}
        mock_eval.assert_called_once_with(dataset_id=ds.id,
                                          user=User.objects.get(username='system'))

    def test_single_failure_continues(self):
        """单个测试集评估失败 → 跳过继续评估其余"""
        self._dataset(name='A')
        self._dataset(name='B')
        User.objects.create_superuser(
            username='system2', password='pass12345', email='sys2@test.com')
        with patch('apps.analytics.offline_eval.run_retrieval_evaluation',
                   side_effect=[RuntimeError('boom'), self._report(recall=0.9)]):
            result = tasks.periodic_retrieval_evaluation()
        assert result['evaluated_datasets'] == 1
        assert result['results'][0]['recall_at_10'] == 0.9


# ============================================================================
# 13. 低分回归：沉淀 + 全链路评估
# ============================================================================
@pytest.mark.django_db
class TestRegressionTasks:
    """siphon_low_score_regression / run_regression_evaluation_task 测试"""

    def test_siphon_disabled(self):
        """沉淀开关关闭 → 跳过"""
        with patch.object(AnalyticsConfig, 'low_score_regression_enabled',
                          return_value=False), \
             patch('apps.analytics.regression_eval.siphon_low_score_qa_to_regression_set') as m:
            result = tasks.siphon_low_score_regression()
        assert result == {'ok': True, 'skipped': True, 'reason': 'disabled'}
        m.assert_not_called()

    def test_siphon_success(self):
        """沉淀成功 → 返回沉淀统计"""
        summary = {'siphoned': 2, 'by_root': {'hr': 2}, 'skipped': 0}
        with patch.object(AnalyticsConfig, 'low_score_regression_enabled',
                          return_value=True), \
             patch('apps.analytics.regression_eval.siphon_low_score_qa_to_regression_set',
                   return_value=summary) as m:
            result = tasks.siphon_low_score_regression()
        assert result == {'ok': True, 'siphoned': 2, 'by_root': {'hr': 2}, 'skipped': 0}
        m.assert_called_once_with()

    def test_siphon_exception(self):
        """沉淀异常 → {'ok': False, 'error': 'siphon_failed'}"""
        with patch.object(AnalyticsConfig, 'low_score_regression_enabled',
                          return_value=True), \
             patch('apps.analytics.regression_eval.siphon_low_score_qa_to_regression_set',
                   side_effect=RuntimeError('x')):
            result = tasks.siphon_low_score_regression()
        assert result == {'ok': False, 'error': 'siphon_failed'}

    def test_eval_disabled(self):
        """评估开关关闭 → 跳过"""
        with patch.object(AnalyticsConfig, 'low_score_regression_enabled',
                          return_value=False):
            result = tasks.run_regression_evaluation_task()
        assert result == {'ok': True, 'skipped': True, 'reason': 'disabled'}

    def test_eval_no_user(self):
        """无 system/超管用户 → 返回错误（mock User 模拟两次 filter 均无结果）"""
        mock_user = MagicMock(name='User')
        mock_user.objects.filter.return_value.first.side_effect = [None, None]
        with patch.object(AnalyticsConfig, 'low_score_regression_enabled',
                          return_value=True), \
             patch('apps.users.models.User', mock_user):
            result = tasks.run_regression_evaluation_task()
        assert result == {'ok': False, 'error': 'no_user_for_eval'}

    def test_eval_success(self):
        """有用户 → 全链路评估并透传结果"""
        User.objects.create_user(
            username='system', password='pass12345', email='sysreg@test.com')
        with patch.object(AnalyticsConfig, 'low_score_regression_enabled',
                          return_value=True), \
             patch('apps.analytics.regression_eval.run_regression_evaluation',
                   return_value={'evaluated': 2, 'passed': 1, 'failed': 1,
                                 'results': []}) as m:
            result = tasks.run_regression_evaluation_task(dataset_id=5, limit=3)
        assert result['ok'] is True
        assert result['evaluated'] == 2
        m.assert_called_once_with(dataset_id=5,
                                  user=User.objects.get(username='system'), limit=3)

    def test_eval_exception(self):
        """评估异常 → {'ok': False, 'error': 'eval_failed'}"""
        User.objects.create_user(
            username='system', password='pass12345', email='sysreg2@test.com')
        with patch.object(AnalyticsConfig, 'low_score_regression_enabled',
                          return_value=True), \
             patch('apps.analytics.regression_eval.run_regression_evaluation',
                   side_effect=RuntimeError('x')):
            result = tasks.run_regression_evaluation_task()
        assert result == {'ok': False, 'error': 'eval_failed'}


# ============================================================================
# 14. 路由决策分析聚合 + Wiki 页面质量评估
# ============================================================================
@pytest.mark.django_db
class TestAggregateRouteAnalysisDaily:
    """aggregate_route_analysis_daily 每日聚合任务"""

    def _make_routed_qa(self, user, session, route_source='wiki'):
        """创建带路由来源的 QA（created_at 为今天，聚合昨天时需回改日期）"""
        qa = QaRecord.objects.create(
            session=session, user=user, question='路由问题', answer='路由回答',
            answer_type='rag', root_type='test_root',
            route_source=route_source,
            route_trace=[{'layer': route_source, 'confidence': 0.8, 'latency_ms': 10}],
            latency_total_ms=150,
        )
        return qa

    @pytest.mark.unit
    def test_aggregates_yesterday(self):
        """传 report_date 字符串 → 聚合该日期并返回统计"""
        from apps.analytics.models import RouteAnalysis

        user = User.objects.create_user(
            username='route_u', password='pass12345', email='route@test.com')
        session = Session.objects.create(user=user, root_type='test_root', title='R')
        qa = self._make_routed_qa(user, session)
        # 改写 created_at 到昨天（auto_now_add 需 update 绕过）
        QaRecord.objects.filter(pk=qa.pk).update(
            created_at=timezone.now() - timedelta(days=1))

        result = tasks.aggregate_route_analysis_daily(report_date=str(_yesterday()))

        assert result['ok'] is True
        assert result['total'] == 1
        assert result['created'] == 1
        assert RouteAnalysis.objects.get(qa_record_id=qa.id).route_source == 'wiki'

    @pytest.mark.unit
    def test_invalid_date_falls_back_to_yesterday(self):
        """非法日期字符串 → 回退昨天聚合，不抛异常"""
        from apps.analytics.models import RouteAnalysis
        user = User.objects.create_user(
            username='route_u2', password='pass12345', email='route2@test.com')
        session = Session.objects.create(user=user, root_type='test_root', title='R')
        qa = self._make_routed_qa(user, session)
        QaRecord.objects.filter(pk=qa.pk).update(
            created_at=timezone.now() - timedelta(days=1))

        result = tasks.aggregate_route_analysis_daily(report_date='not-a-date')

        assert result['ok'] is True
        assert RouteAnalysis.objects.filter(qa_record_id=qa.id).count() == 1

    @pytest.mark.unit
    def test_aggregation_exception(self):
        """聚合异常 → {'ok': False, 'error': 'aggregation_failed'}"""
        with patch('apps.analytics.utils.aggregate_route_analysis',
                   side_effect=RuntimeError('db down')):
            result = tasks.aggregate_route_analysis_daily()
        assert result == {'ok': False, 'error': 'aggregation_failed'}


@pytest.mark.django_db
class TestBatchEvaluateWikiQuality:
    """batch_evaluate_wiki_quality 批量评估任务"""

    def _make_published_page(self, title='测试页', node=None):
        """创建已发布 Wiki 页面（node 传 None 时为社区页）"""
        from apps.wiki.models import WikiPage
        return WikiPage.objects.create(
            title=title, node=node, status='published', content='Wiki 正文内容')

    def test_evaluates_published_node_pages(self):
        """仅评估 node 挂载型已发布页面，返回分类计数"""
        from apps.knowledge.models import KnowledgeNode
        from apps.users.models import User
        user = User.objects.create_user(
            username='wiki_u', password='pass12345', email='wiki@test.com')
        node = KnowledgeNode.objects.create(
            name='node', node_type='folder', root_type='test_root', created_by=user)
        self._make_published_page(node=node)
        self._make_published_page(node=node)

        with patch('apps.analytics.wiki_eval.evaluate_wiki_page',
                   return_value={'ok': True, 'evaluated': ['faithfulness', 'completeness']}):
            result = tasks.batch_evaluate_wiki_quality()

        assert result['ok'] is True
        assert result['evaluated'] == 2
        assert result['failed'] == 0

    def test_skipped_pages_not_failed(self):
        """评估器返回 skipped(无源切片等) → 计入 skipped 而非 failed"""
        from apps.knowledge.models import KnowledgeNode
        from apps.users.models import User
        user = User.objects.create_user(
            username='wiki_u2', password='pass12345', email='wiki2@test.com')
        # node 挂载型页面（通过任务查询），但评估器判定无源切片跳过
        node = KnowledgeNode.objects.create(
            name='node2', node_type='folder', root_type='test_root', created_by=user)
        self._make_published_page(title='无源切片页', node=node)

        with patch('apps.analytics.wiki_eval.evaluate_wiki_page',
                   return_value={'ok': False, 'skipped': 'no_source_chunks'}):
            result = tasks.batch_evaluate_wiki_quality()

        assert result['ok'] is True
        assert result['evaluated'] == 0
        assert result['skipped'] == 1
        assert result['failed'] == 0

    def test_limit_restricts_page_count(self):
        """limit 限制单次评估页面数"""
        from apps.knowledge.models import KnowledgeNode
        from apps.users.models import User
        user = User.objects.create_user(
            username='wiki_u3', password='pass12345', email='wiki3@test.com')
        node = KnowledgeNode.objects.create(
            name='node3', node_type='folder', root_type='test_root', created_by=user)
        self._make_published_page(title='p1', node=node)
        self._make_published_page(title='p2', node=node)

        with patch('apps.analytics.wiki_eval.evaluate_wiki_page',
                   return_value={'ok': True, 'evaluated': []}):
            result = tasks.batch_evaluate_wiki_quality(limit=1)

        assert result['ok'] is True
        assert result['evaluated'] == 1
