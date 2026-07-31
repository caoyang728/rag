"""analytics serializers

为 RAG 质量评估相关接口提供统一的序列化器，替代 View 中手动循环构造 dict 的写法。
- 字段集中管理，便于复用与维护
- 计算字段（annotate / select_related 衍生）通过 SerializerMethodField 或只读字段暴露
- 输出格式与原手动构造的 dict 保持一致，前端无需改动
"""
from rest_framework import serializers

from apps.analytics.models import (
    GoldenDataset, GoldenQuestion,
    DocumentQualityReport,
)


class GoldenDatasetSerializer(serializers.ModelSerializer):
    """黄金测试集序列化器

    对应 GoldenDatasetListView / GoldenDatasetDetailView 的列表与详情返回。
    """

    class Meta:
        model = GoldenDataset
        fields = [
            'id', 'name', 'description', 'root_type', 'status',
            'question_count', 'version', 'created_at', 'updated_at',
        ]


class GoldenQuestionSerializer(serializers.ModelSerializer):
    """黄金测试问题序列化器

    用于 GoldenDatasetDetailView 的 questions 列表。
    - relevant_doc_count: 由 View 中 annotate(Count('relevant_docs')) 提供，
      属于运行时计算字段，不对应 model 字段，用只读 IntegerField 接收。
    - has_reference: 由 View 中 select_related('reference_answer') 判断是否存在，
      OneToOne 反向关系无关联时 hasattr 返回 False，这里用 SerializerMethodField
      保持原有判断逻辑，避免 DRF 试图从不存在的字段取值。
    """
    relevant_doc_count = serializers.IntegerField(read_only=True, default=0)
    has_reference = serializers.SerializerMethodField()

    class Meta:
        model = GoldenQuestion
        fields = [
            'id', 'question', 'question_type', 'difficulty', 'tags', 'order',
            'relevant_doc_count', 'has_reference',
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
