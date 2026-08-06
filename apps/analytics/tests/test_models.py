"""
apps.analytics.models 单元测试

覆盖范围：
- 关键字段的默认值 / choices / 帮助文案语义
- 数据库级唯一约束：KeywordWeight(keyword,root_type)、AccuracyReport(report_date)、
  OrgUsageReport(report_date,department_id,team_id)、QueueDepthLog(queue_name,minute_bucket)、
  MultiDimensionScore(qa_record_id,dimension)、LowScoreAnalysis OneToOne
- 模型间关联：GoldenDataset→GoldenQuestion→GoldenRelevantDoc/GoldenReferenceAnswer、
  DocumentQualityReport OneToOne Document、RetrievalQualityReport→GoldenDataset

说明：约束测试通过真实 INSERT 触发 IntegrityError 验证（数据库层兜底，
不依赖应用层校验），符合"关键唯一性由 DB 约束保障"的既定设计。
"""
import pytest
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.analytics.models import (
    KeywordWeight, AccuracyReport, SystemMetricsReport, OrgUsageReport,
    QueueDepthLog, GoldenDataset, GoldenQuestion, GoldenRelevantDoc,
    GoldenReferenceAnswer, MultiDimensionScore, DocumentQualityReport,
    RetrievalQualityReport, CoverageReport, LowScoreAnalysis, RouteAnalysis,
)


def _make_user(username='m_user'):
    """创建测试用户（模型关联用）"""
    from apps.users.models import User
    return User.objects.create_user(
        username=username, email=f'{username}@test.com', password='pass12345')


@pytest.mark.django_db
class TestKeywordWeight:
    """关键词权重：默认值 + (keyword, root_type) 唯一约束"""

    @pytest.mark.integration
    def test_defaults(self):
        kw = KeywordWeight.objects.create(keyword='报销')
        assert kw.root_type == 'all'
        assert kw.weight_score == 1.0
        assert kw.hit_count == 0
        assert kw.good_feedback == 0
        assert kw.bad_feedback == 0
        assert kw.last_hit_at is None

    @pytest.mark.integration
    def test_unique_together_keyword_root_type(self):
        KeywordWeight.objects.create(keyword='报销', root_type='all')
        # atomic 子块：IntegrityError 在子事务中触发后回滚，避免外层事务进入 broken 状态
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                KeywordWeight.objects.create(keyword='报销', root_type='all')
        # 不同 root_type 允许共存
        KeywordWeight.objects.create(keyword='报销', root_type='hr')


@pytest.mark.django_db
class TestAccuracyReport:
    """准确率日报：report_date 全局唯一"""

    @pytest.mark.integration
    def test_report_date_unique(self):
        AccuracyReport.objects.create(report_date=datetime(2026, 1, 1).date())
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                AccuracyReport.objects.create(report_date=datetime(2026, 1, 1).date())

    @pytest.mark.integration
    def test_defaults(self):
        report = AccuracyReport.objects.create(report_date=datetime(2026, 1, 2).date())
        assert report.total_qa == 0
        assert report.accuracy_rate == 0.0
        assert report.total_tokens == 0
        assert report.top_bad_tags == []
        assert report.top_root_types == []


@pytest.mark.django_db
class TestSystemMetricsReport:
    """系统指标日报：report_date 唯一 + JSON 字段默认值"""

    @pytest.mark.integration
    def test_defaults(self):
        report = SystemMetricsReport.objects.create(
            report_date=datetime(2026, 1, 1).date())
        assert report.cache_hit_rate == 0.0
        assert report.llm_success_rate == 0.0
        assert report.latency_histogram == {}
        assert report.error_distribution == {}

    @pytest.mark.integration
    def test_report_date_unique(self):
        SystemMetricsReport.objects.create(report_date=datetime(2026, 1, 1).date())
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                SystemMetricsReport.objects.create(report_date=datetime(2026, 1, 1).date())


@pytest.mark.django_db
class TestOrgUsageReport:
    """组织使用报表：(report_date, department_id, team_id) 唯一（-1 哨兵防 NULL 冲突）"""

    @pytest.mark.integration
    def test_unique_together_with_neg1_sentinel(self):
        OrgUsageReport.objects.create(
            report_date=datetime(2026, 1, 1).date(),
            department_id=1, team_id=-1)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                OrgUsageReport.objects.create(
                    report_date=datetime(2026, 1, 1).date(),
                    department_id=1, team_id=-1)
        # 不同团队明细可共存
        OrgUsageReport.objects.create(
            report_date=datetime(2026, 1, 1).date(),
            department_id=1, team_id=10)

    @pytest.mark.integration
    def test_team_id_default_neg1(self):
        """team_id 默认 -1 表示部门级汇总"""
        report = OrgUsageReport.objects.create(
            report_date=datetime(2026, 1, 1).date(), department_id=2)
        assert report.team_id == -1


@pytest.mark.django_db
class TestQueueDepthLog:
    """队列深度快照：(queue_name, minute_bucket) 唯一防 Beat 重入"""

    @pytest.mark.integration
    def test_unique_together_queue_and_minute(self):
        bucket = timezone.now().replace(second=0, microsecond=0)
        QueueDepthLog.objects.create(queue_name='default', depth=1, minute_bucket=bucket)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                QueueDepthLog.objects.create(queue_name='default', depth=2, minute_bucket=bucket)
        # 不同队列同一分钟可共存
        QueueDepthLog.objects.create(queue_name='parse', depth=3, minute_bucket=bucket)

    @pytest.mark.integration
    def test_defaults(self):
        log = QueueDepthLog.objects.create(queue_name='analytics')
        assert log.depth == 0
        assert log.worker_count == 0
        assert log.avg_wait_sec == 0.0
        assert log.task_types == []


@pytest.mark.django_db
class TestGoldenDatasetAndQuestions:
    """黄金测试集模型：choices / 类型默认值 / 关联关系"""

    @pytest.mark.integration
    def test_dataset_type_default_custom(self):
        """dataset_type 默认 custom，历史数据无需迁移即归入自定义类型"""
        ds = GoldenDataset.objects.create(name='默认集')
        assert ds.dataset_type == 'custom'
        assert ds.status == 'active'
        assert ds.root_type == 'company_doc'
        assert ds.version == 'v1'
        assert ds.question_count == 0

    @pytest.mark.integration
    def test_question_relations_and_regression_fields(self):
        user = _make_user('gd_user')
        ds = GoldenDataset.objects.create(name='回归集', created_by=user)
        q = GoldenQuestion.objects.create(
            dataset=ds, question='低分问题', source_qa_record_id=42,
            pass_count=1, last_eval_at=timezone.now())
        ref = GoldenReferenceAnswer.objects.create(
            question=q, reference_answer='标准答案', created_by=user)
        assert q.reference_answer == ref
        assert ds.questions.count() == 1
        # 相关文档标注 related_name
        from apps.knowledge.models import KnowledgeNode, Document
        node = KnowledgeNode.objects.create(
            name='gd_root', node_type='root', root_type='test_root', created_by=user)
        doc = Document.objects.create(
            node=node, owner=user, title='相关文档', file_name='d.txt',
            file_type='txt', file_hash='h', root_type='test_root', status='done', dept_id=1)
        rel = GoldenRelevantDoc.objects.create(
            question=q, document=doc, relevance_level='high', note='高度相关')
        assert q.relevant_docs.count() == 1
        assert rel.relevance_level == 'high'
        assert doc.golden_annotations.count() == 1

    @pytest.mark.integration
    def test_relevance_choices_validated_by_full_clean(self):
        """relevance_level 非法值触发应用层 choices 校验（模型无 DB CheckConstraint）"""
        user = _make_user('gd_user2')
        ds = GoldenDataset.objects.create(name='集')
        q = GoldenQuestion.objects.create(dataset=ds, question='q')
        from apps.knowledge.models import KnowledgeNode, Document
        node = KnowledgeNode.objects.create(
            name='gd_root2', node_type='root', root_type='test_root', created_by=user)
        doc = Document.objects.create(
            node=node, owner=user, title='d', file_name='d.txt',
            file_type='txt', file_hash='h2', root_type='test_root', status='done', dept_id=1)
        rel = GoldenRelevantDoc(question=q, document=doc, relevance_level='invalid')
        with pytest.raises(ValidationError):
            rel.full_clean()


@pytest.mark.django_db
class TestMultiDimensionScore:
    """多维度评估：维度 choices 唯一约束 (qa_record_id, dimension)"""

    def _make_qa(self, user):
        from apps.memory.models import Session
        from apps.chat.models import QaRecord
        session = Session.objects.create(user=user, root_type='test_root', title='s')
        return QaRecord.objects.create(
            session=session, user=user, question='q', answer='a',
            answer_type='rag', root_type='test_root',
            is_hit_cache=False, is_success=True, error_type='',
            tokens_prompt=10, tokens_completion=5, cost_estimate=0.01,
            latency_total_ms=100, latency_llm_ms=50,
            latency_retrieval_ms=20, latency_ttfb_ms=30, tokens_per_second=5.0)

    @pytest.mark.integration
    def test_unique_qa_dimension(self):
        user = _make_user('mds_user')
        qa = self._make_qa(user)
        MultiDimensionScore.objects.create(qa_record=qa, dimension='faithfulness', score=0.8)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                MultiDimensionScore.objects.create(qa_record=qa, dimension='faithfulness', score=0.6)
        # 不同维度可共存
        MultiDimensionScore.objects.create(qa_record=qa, dimension='toxicity', score=0.9)

    @pytest.mark.integration
    def test_defaults_and_history_dimension_compat(self):
        user = _make_user('mds_user2')
        qa = self._make_qa(user)
        score = MultiDimensionScore.objects.create(qa_record=qa, dimension='context_recall')
        assert score.score == 0.0
        assert score.status == 'pending'
        assert score.atomic_facts == []
        assert score.eval_model == 'deepseek-chat'


@pytest.mark.django_db
class TestDocumentQualityReport:
    """文档质量报告：OneToOne 绑定 Document"""

    @pytest.mark.integration
    def test_one_to_one_document(self):
        user = _make_user('dqr_user')
        from apps.knowledge.models import KnowledgeNode, Document
        node = KnowledgeNode.objects.create(
            name='dqr_root', node_type='root', root_type='test_root', created_by=user)
        doc = Document.objects.create(
            node=node, owner=user, title='文档', file_name='doc.txt',
            file_type='txt', file_hash='h', root_type='test_root', status='done', dept_id=1)
        report = DocumentQualityReport.objects.create(
            document=doc, parse_status='success', quality_score=90.0)
        assert doc.quality_report == report
        # 同一 Document 二次创建触发 OneToOne 唯一约束
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                DocumentQualityReport.objects.create(document=doc, quality_score=80.0)


@pytest.mark.django_db
class TestRetrievalQualityReport:
    """检索质量报告：关联测试集 + 默认值"""

    @pytest.mark.integration
    def test_relation_and_defaults(self):
        ds = GoldenDataset.objects.create(name='检索集')
        report = RetrievalQualityReport.objects.create(dataset=ds, status='pending')
        assert report.recall_at_5 == 0.0
        assert report.mrr == 0.0
        assert report.total_questions == 0
        assert report.config_snapshot == {}
        assert ds.retrieval_reports.count() == 1


@pytest.mark.django_db
class TestCoverageReport:
    """覆盖率报告：字段默认值"""

    @pytest.mark.integration
    def test_defaults(self):
        report = CoverageReport.objects.create(report_date=datetime(2026, 1, 1).date())
        assert report.total_hot_queries == 0
        assert report.hot_query_coverage_rate == 0.0
        assert report.gap_queries == []
        assert report.domain_coverage == {}
        assert report.feedback_loop_count == 0


@pytest.mark.django_db
class TestLowScoreAnalysis:
    """低分归因：OneToOne 绑定 QaRecord + choices 默认值"""

    def _make_qa(self, user):
        from apps.memory.models import Session
        from apps.chat.models import QaRecord
        session = Session.objects.create(user=user, root_type='test_root', title='s')
        return QaRecord.objects.create(
            session=session, user=user, question='q', answer='a',
            answer_type='rag', root_type='test_root',
            is_hit_cache=False, is_success=True, error_type='',
            tokens_prompt=10, tokens_completion=5, cost_estimate=0.01,
            latency_total_ms=100, latency_llm_ms=50,
            latency_retrieval_ms=20, latency_ttfb_ms=30, tokens_per_second=5.0)

    @pytest.mark.integration
    def test_one_to_one_qa_record(self):
        user = _make_user('lsa_m_user')
        qa = self._make_qa(user)
        analysis = LowScoreAnalysis.objects.create(qa_record=qa)
        assert qa.low_score_analysis == analysis
        assert analysis.root_cause_category == 'unknown'
        assert analysis.affected_layer == 'unknown'
        assert analysis.analysis_method == 'rule'
        assert analysis.status == 'pending'
        assert analysis.low_dimensions == []
        assert analysis.suggestions == []
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                LowScoreAnalysis.objects.create(qa_record=qa)


@pytest.mark.django_db
class TestRouteAnalysis:
    """路由决策分析：字段默认值"""

    @pytest.mark.integration
    def test_defaults(self):
        route = RouteAnalysis.objects.create(
            question='路由问题', route_source='rag', confidence=0.8)
        assert route.route_trace == []
        assert route.latency_ms == 0
        assert route.answer_quality is None
        assert route.answer_quality is None or isinstance(route.answer_quality, float)
