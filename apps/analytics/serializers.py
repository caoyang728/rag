"""analytics serializers

为 RAG 质量评估相关接口提供统一的序列化器，替代 View 中手动循环构造 dict 的写法。
- 字段集中管理，便于复用与维护
- 计算字段（annotate / select_related 衍生）通过 SerializerMethodField 或只读字段暴露
- 输出格式与原手动构造的 dict 保持一致，前端无需改动
"""
from rest_framework import serializers

from apps.analytics.models import (
    GoldenDataset, GoldenQuestion,
    DocumentQualityReport, LowScoreAnalysis,
)


class GoldenDatasetSerializer(serializers.ModelSerializer):
    """黄金测试集序列化器

    对应 GoldenDatasetListView / GoldenDatasetDetailView 的列表与详情返回。
    - dataset_type_label: choices 中文展示名,前端直接渲染无需维护映射表
    """

    dataset_type_label = serializers.SerializerMethodField()

    class Meta:
        model = GoldenDataset
        fields = [
            'id', 'name', 'description', 'root_type', 'status',
            'dataset_type', 'dataset_type_label',
            'question_count', 'version', 'created_at', 'updated_at',
        ]

    def get_dataset_type_label(self, obj):
        return obj.get_dataset_type_display()


class GoldenQuestionSerializer(serializers.ModelSerializer):
    """黄金测试问题序列化器

    用于 GoldenDatasetDetailView 的 questions 列表。
    - relevant_doc_count: 由 View 中 annotate(Count('relevant_docs')) 提供，
      属于运行时计算字段，不对应 model 字段，用只读 IntegerField 接收。
    - has_reference: 由 View 中 select_related('reference_answer') 判断是否存在，
      OneToOne 反向关系无关联时 hasattr 返回 False，这里用 SerializerMethodField
      保持原有判断逻辑，避免 DRF 试图从不存在的字段取值。
    - pass_count / source_qa_record_id / last_eval_at: 低分回归专用字段,
      自定义测试集保持默认值(0/null),前端按 dataset_type 决定是否展示。
    """
    relevant_doc_count = serializers.IntegerField(read_only=True, default=0)
    has_reference = serializers.SerializerMethodField()

    class Meta:
        model = GoldenQuestion
        fields = [
            'id', 'question', 'question_type', 'difficulty', 'tags', 'order',
            'relevant_doc_count', 'has_reference',
            'source_qa_record_id', 'pass_count', 'last_eval_at',
        ]

    def get_has_reference(self, obj):
        # OneToOne 反向关系：无关联时 hasattr 捕获 DoesNotExist 返回 False
        return hasattr(obj, 'reference_answer') and obj.reference_answer is not None


class DocumentQualityReportSerializer(serializers.ModelSerializer):
    """文档质量报告序列化器

    对应 DocumentQualityReportListView 的列表返回。
    - document_name: 取自关联 Document 的 file_name，View 中已 select_related，
      避免序列化时触发 N+1 查询。
    - quality_issues: 截取前 5 条，避免响应体过大。
    - evaluated_at: 原手动实现为 .isoformat() 或空串，这里用 SerializerMethodField
      保持完全一致（DateTimeField 对 None 会返回 None，与原空串行为不同）。
    """
    document_name = serializers.SerializerMethodField()
    quality_issues = serializers.SerializerMethodField()
    evaluated_at = serializers.SerializerMethodField()

    class Meta:
        model = DocumentQualityReport
        fields = [
            'id', 'document_id', 'document_name', 'quality_score',
            'parse_status', 'text_extraction_rate', 'chunk_count',
            'avg_chunk_chars', 'embedding_success_rate', 'quality_issues',
            'evaluated_at',
        ]

    def get_document_name(self, obj):
        return obj.document.file_name if obj.document else ''

    def get_quality_issues(self, obj):
        return obj.quality_issues[:5] if obj.quality_issues else []

    def get_evaluated_at(self, obj):
        return obj.evaluated_at.isoformat() if obj.evaluated_at else ''


class LowScoreAnalysisSerializer(serializers.ModelSerializer):
    """低分归因分析序列化器

    用于归因列表 / 详情接口。
    - question / answer: 取自关联 QaRecord,View 中已 select_related,避免 N+1
    - root_type / qa_created_at: 同上,便于前端展示不额外查 QaRecord
    - category_label / layer_label / method_label: choices 的中文展示名,
      用 SerializerMethodField 走 get_*_display(),避免前端维护映射表
    """
    question = serializers.SerializerMethodField()
    answer = serializers.SerializerMethodField()
    root_type = serializers.SerializerMethodField()
    qa_created_at = serializers.SerializerMethodField()
    category_label = serializers.SerializerMethodField()
    layer_label = serializers.SerializerMethodField()
    method_label = serializers.SerializerMethodField()
    status_label = serializers.SerializerMethodField()

    class Meta:
        model = LowScoreAnalysis
        fields = [
            'id', 'qa_record_id', 'question', 'answer', 'root_type', 'qa_created_at',
            'avg_score', 'threshold',
            'root_cause_category', 'category_label',
            'root_cause_detail', 'affected_layer', 'layer_label',
            'low_dimensions', 'diagnosis', 'suggestions',
            'analysis_method', 'method_label', 'analysis_model',
            'analysis_tokens_used', 'analysis_cost', 'analysis_latency_ms',
            'status', 'status_label', 'error_message', 'created_at', 'updated_at',
        ]

    def get_question(self, obj):
        # 截断 80 字,完整内容由详情接口提供
        return (obj.qa_record.question or '')[:80] if obj.qa_record else ''

    def get_answer(self, obj):
        return (obj.qa_record.answer or '')[:120] if obj.qa_record else ''

    def get_root_type(self, obj):
        return obj.qa_record.root_type if obj.qa_record else ''

    def get_qa_created_at(self, obj):
        return obj.qa_record.created_at.isoformat() if obj.qa_record else ''

    def get_category_label(self, obj):
        return obj.get_root_cause_category_display()

    def get_layer_label(self, obj):
        return obj.get_affected_layer_display()

    def get_method_label(self, obj):
        return obj.get_analysis_method_display()

    def get_status_label(self, obj):
        return obj.get_status_display()
