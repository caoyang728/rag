"""
apps.analytics.serializers 单元测试

覆盖范围：
- GoldenDatasetSerializer：字段映射 + dataset_type_label 中文展示名
- GoldenQuestionSerializer：relevant_doc_count 运行时字段 + has_reference
  （有/无 OneToOne reference_answer 的两种分支）
- DocumentQualityReportSerializer：document_name 关联取值 / quality_issues
  截断前 5 条 / evaluated_at 的 isoformat 与空串两种输出
- LowScoreAnalysisSerializer：question/answer 截断 + 关联 QaRecord 字段 +
  category/layer/method/status 的 choices 中文展示名

说明：与 View 中 annotate(Count('relevant_docs')) / select_related('reference_answer')
的用法对齐，测试同样通过 ORM annotate/select_related 构造序列化输入。
"""
import pytest

from django.db import models

from apps.analytics.models import (
    GoldenDataset, GoldenQuestion, GoldenReferenceAnswer,
    DocumentQualityReport, LowScoreAnalysis,
)
from apps.analytics.serializers import (
    GoldenDatasetSerializer, GoldenQuestionSerializer,
    DocumentQualityReportSerializer, LowScoreAnalysisSerializer,
)


@pytest.mark.django_db
class TestGoldenDatasetSerializer:
    """黄金测试集序列化器：字段完整性 + dataset_type_label 展示名"""

    @pytest.mark.integration
    def test_serialize_returns_all_fields_and_type_label(self):
        ds = GoldenDataset.objects.create(
            name='HR 测试集', description='人工标注',
            root_type='company_doc', status='active',
            dataset_type='custom', question_count=5, version='v2',
        )
        data = GoldenDatasetSerializer(ds).data
        for key in ['id', 'name', 'description', 'root_type', 'status',
                    'dataset_type', 'dataset_type_label', 'question_count',
                    'version', 'created_at', 'updated_at']:
            assert key in data, f'缺少字段 {key}'
        assert data['name'] == 'HR 测试集'
        assert data['dataset_type'] == 'custom'
        # choices 中文展示名：前端直接渲染，无需维护映射表
        assert data['dataset_type_label'] == '自定义'
        assert data['question_count'] == 5
        assert data['version'] == 'v2'
        assert data['created_at'] is not None

    @pytest.mark.integration
    def test_dataset_type_label_for_regression(self):
        """低分回归类型的中文展示名"""
        ds = GoldenDataset.objects.create(
            name='回归集', dataset_type='regression_low_score')
        data = GoldenDatasetSerializer(ds).data
        assert data['dataset_type_label'] == '低分回归'


@pytest.mark.django_db
class TestGoldenQuestionSerializer:
    """黄金测试问题序列化器：运行时计算字段 has_reference / relevant_doc_count"""

    def _make_dataset_and_questions(self):
        ds = GoldenDataset.objects.create(name='测试集')
        q1 = GoldenQuestion.objects.create(
            dataset=ds, question='如何办理入职？', question_type='procedural',
            difficulty='medium', tags=['HR', '入职'], order=1)
        q2 = GoldenQuestion.objects.create(
            dataset=ds, question='年假政策是什么？', question_type='factoid',
            difficulty='easy', tags=[], order=2)
        GoldenReferenceAnswer.objects.create(
            question=q1, reference_answer='联系 HR 办理',
            reference_answer_source='manual', key_points=['HR'])
        return ds, q1, q2

    @pytest.mark.integration
    def test_has_reference_branches(self):
        """有参考答案 → True；无 → False（OneToOne 反向关系 DoesNotExist 兜底）"""
        ds, q1, q2 = self._make_dataset_and_questions()
        qs = (
            GoldenQuestion.objects.filter(dataset=ds)
            .order_by('order')
            .select_related('reference_answer')
            .annotate(relevant_doc_count=models.Count('relevant_docs'))
        )
        data = GoldenQuestionSerializer(qs, many=True).data
        by_question = {row['question']: row for row in data}
        assert by_question['如何办理入职？']['has_reference'] is True
        assert by_question['年假政策是什么？']['has_reference'] is False

    @pytest.mark.integration
    def test_relevant_doc_count_defaults_zero(self):
        """无相关文档标注时 relevant_doc_count 为 0（annotate 提供）"""
        ds, q1, q2 = self._make_dataset_and_questions()
        qs = (
            GoldenQuestion.objects.filter(dataset=ds)
            .select_related('reference_answer')
            .annotate(relevant_doc_count=models.Count('relevant_docs'))
        )
        data = GoldenQuestionSerializer(qs, many=True).data
        assert all(row['relevant_doc_count'] == 0 for row in data)

    @pytest.mark.integration
    def test_low_score_regression_fields_preserved(self):
        """低分回归专用字段原样透传（source_qa_record_id/pass_count/last_eval_at）"""
        ds = GoldenDataset.objects.create(name='回归集')
        q = GoldenQuestion.objects.create(
            dataset=ds, question='低分问题', source_qa_record_id=123,
            pass_count=2, last_eval_at='2026-01-01T00:00:00Z')
        data = GoldenQuestionSerializer(q).data
        assert data['source_qa_record_id'] == 123
        assert data['pass_count'] == 2
        assert data['last_eval_at'] == '2026-01-01T00:00:00Z'


@pytest.mark.django_db
class TestDocumentQualityReportSerializer:
    """文档质量报告序列化器：关联名称 / issues 截断 / evaluated_at 输出"""

    def _make_doc_quality_report(self, evaluated_at=None, issues=None):
        from apps.users.models import User
        from apps.knowledge.models import KnowledgeNode, Document
        user = User.objects.create_user(
            username='dq_user', email='dq@test.com', password='pass12345')
        node = KnowledgeNode.objects.create(
            name='dq_root', node_type='root', root_type='test_root',
            created_by=user)
        doc = Document.objects.create(
            node=node, owner=user, title='质量文档', file_name='quality.txt',
            file_type='txt', file_hash='h1', root_type='test_root',
            status='done', dept_id=1)
        return DocumentQualityReport.objects.create(
            document=doc, parse_status='success', text_extraction_rate=0.9,
            chunk_count=10, avg_chunk_chars=500,
            embedding_success_rate=0.95, quality_score=88.0,
            quality_issues=issues or [], evaluated_at=evaluated_at,
        )

    @pytest.mark.integration
    def test_document_name_and_evaluated_at_iso(self):
        """document_name 取关联 file_name；evaluated_at 输出 isoformat"""
        from datetime import datetime
        from django.utils import timezone
        report = self._make_doc_quality_report(
            evaluated_at=timezone.make_aware(datetime(2026, 1, 2, 3, 4, 5)))
        data = DocumentQualityReportSerializer(report).data
        assert data['document_name'] == 'quality.txt'
        assert data['document_id'] == report.document_id
        assert data['quality_score'] == 88.0
        assert data['evaluated_at'] == '2026-01-02T03:04:05+08:00' or data['evaluated_at'].startswith('2026-01-02T03:04:05')

    @pytest.mark.integration
    def test_quality_issues_truncated_to_five(self):
        """quality_issues 截断为前 5 条，避免响应体过大"""
        issues = [{'level': 'warning', 'type': f't_{i}', 'detail': 'd'} for i in range(8)]
        report = self._make_doc_quality_report(issues=issues)
        data = DocumentQualityReportSerializer(report).data
        assert len(data['quality_issues']) == 5
        assert data['quality_issues'][0]['type'] == 't_0'

    @pytest.mark.integration
    def test_evaluated_at_none_returns_empty_string(self):
        """evaluated_at 为空时输出空串（与原手动实现保持一致）"""
        report = self._make_doc_quality_report(evaluated_at=None)
        data = DocumentQualityReportSerializer(report).data
        assert data['evaluated_at'] == ''


@pytest.mark.django_db
class TestLowScoreAnalysisSerializer:
    """低分归因序列化器：关联字段截断 + choices 中文展示名"""

    def _make_analysis(self):
        from apps.users.models import User
        from apps.memory.models import Session
        from apps.chat.models import QaRecord
        user = User.objects.create_user(
            username='lsa_user', email='lsa@test.com', password='pass12345')
        session = Session.objects.create(
            user=user, root_type='test_root', title='归因会话')
        qa = QaRecord.objects.create(
            session=session, user=user,
            question='长问题' * 40, answer='长回答' * 80,
            answer_type='rag', root_type='test_root',
            is_hit_cache=False, is_success=True, error_type='',
            tokens_prompt=100, tokens_completion=50,
            cost_estimate=0.01, latency_total_ms=500,
            latency_llm_ms=300, latency_retrieval_ms=100,
            latency_ttfb_ms=200, tokens_per_second=10.0,
        )
        return LowScoreAnalysis.objects.create(
            qa_record=qa, avg_score=0.3, threshold=0.5,
            root_cause_category='retrieval_recall', root_cause_detail='detail',
            affected_layer='retrieval',
            low_dimensions=[{'dimension': 'faithfulness', 'score': 0.3}],
            diagnosis='召回不足', suggestions=[{'type': 'short_term', 'action': 'a'}],
            analysis_method='rule', status='completed',
            analysis_model='deepseek-chat', analysis_tokens_used=100,
            analysis_cost=0.001, analysis_latency_ms=500,
        )

    @pytest.mark.integration
    def test_question_answer_truncated(self):
        """question 截断 80 字、answer 截断 120 字"""
        analysis = self._make_analysis()
        data = LowScoreAnalysisSerializer(analysis).data
        assert len(data['question']) == 80
        assert len(data['answer']) == 120

    @pytest.mark.integration
    def test_qa_related_fields(self):
        """root_type / qa_created_at 取自关联 QaRecord"""
        analysis = self._make_analysis()
        data = LowScoreAnalysisSerializer(analysis).data
        assert data['root_type'] == 'test_root'
        assert data['qa_created_at'].startswith(analysis.qa_record.created_at.isoformat()[:10])

    @pytest.mark.integration
    def test_choices_labels(self):
        """category/layer/method/status 的 choices 中文展示名"""
        analysis = self._make_analysis()
        data = LowScoreAnalysisSerializer(analysis).data
        assert data['category_label'] == '检索召回不足'
        assert data['layer_label'] == '检索层'
        assert data['method_label'] == '规则归因'
        assert data['status_label'] == '已完成'
