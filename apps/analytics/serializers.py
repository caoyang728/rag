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
    OrgUsageReport, KeywordFeedbackAgg,
)
from apps.chat.models import QaRecord, QaFeedback


class LenientIntField(serializers.IntegerField):
    """宽松整数字段：解析失败回退默认值，超范围钳位到 [min_value, max_value]

    评估看板历史参数校验是宽松的（非法值回退默认、越界钳位），
    前端契约不允许因非法参数直接 400，故统一用该字段替代视图内 try/int 手动解析。
    """

    def __init__(self, fallback, min_value=None, max_value=None, **kwargs):
        self.fallback = fallback
        self._lo = min_value
        self._hi = max_value
        kwargs.update({'required': False, 'default': fallback})
        super().__init__(**kwargs)

    def run_validation(self, data=serializers.empty):
        if data is serializers.empty or data in (None, ''):
            return self.fallback
        try:
            value = int(data)
        except (ValueError, TypeError):
            return self.fallback
        if self._lo is not None:
            value = max(value, self._lo)
        if self._hi is not None:
            value = min(value, self._hi)
        return value


class LenientFloatField(serializers.FloatField):
    """宽松浮点字段：解析失败回退默认值（threshold 等参数用）"""

    def __init__(self, fallback, **kwargs):
        self.fallback = fallback
        kwargs.update({'required': False, 'default': fallback})
        super().__init__(**kwargs)

    def run_validation(self, data=serializers.empty):
        if data is serializers.empty or data in (None, ''):
            return self.fallback
        try:
            return float(data)
        except (ValueError, TypeError):
            return self.fallback


class LenientOrgIdField(serializers.IntegerField):
    """宽松组织 ID 字段：非法值置 None，不报错

    组织筛选参数 dept_id/team_id 采用宽松策略：脏数据/非法值一律视为未传
    （不拦截请求），与原 _parse_org_scope 的"非法值置 None"行为保持一致。
    """

    def run_validation(self, data=serializers.empty):
        if data is serializers.empty or data in (None, ''):
            return None
        try:
            return int(data)
        except (ValueError, TypeError):
            return None


class EvalDashboardOverviewQuerySerializer(serializers.Serializer):
    """评估看板 overview 查询参数（宽松解析）

    days 非法回退 7、越界钳位 1-90；root_type 空串表示全部；
    dept_id/team_id 非法值置 None。
    """

    days = LenientIntField(fallback=7, min_value=1, max_value=90)
    root_type = serializers.CharField(required=False, allow_blank=True, default='')
    dept_id = LenientOrgIdField()
    team_id = LenientOrgIdField()


class EvalDashboardTrendQuerySerializer(EvalDashboardOverviewQuerySerializer):
    """评估看板 trend 查询参数（在 overview 基础上增加 dimension）"""

    dimension = serializers.CharField(required=False, allow_blank=True, default='')


class EvalDashboardLowScoreQuerySerializer(serializers.Serializer):
    """评估看板 low-score-qa 查询参数（宽松解析）

    limit 非法回退 20、钳位 1-100；threshold 非法回退 0.5。
    """

    days = LenientIntField(fallback=7, min_value=1, max_value=90)
    root_type = serializers.CharField(required=False, allow_blank=True, default='')
    dept_id = LenientOrgIdField()
    team_id = LenientOrgIdField()
    limit = LenientIntField(fallback=20, min_value=1, max_value=100)
    threshold = LenientFloatField(fallback=0.5)


class RouteAnalysisQuerySerializer(serializers.Serializer):
    """路由分析看板查询参数（宽松解析）"""

    days = LenientIntField(fallback=7, min_value=1, max_value=90)
    dept_id = LenientOrgIdField()
    team_id = LenientOrgIdField()


class QaRecordSerializer(serializers.ModelSerializer):
    """QA 记录列表/详情行序列化器

    替代 QaRecordView 中手动构造 dict：
    - rating: 来自 OneToOne feedback，无反馈时为 0（select_related LEFT JOIN 后为 None）
    - cost_estimate: DecimalField 转 float，与原手动 float() 行为一致
    - created_at: 用 SerializerMethodField 输出 UTC isoformat，避免 DRF 按
      TIME_ZONE 转成上海时区导致 wire 格式与历史数据不一致
    """

    rating = serializers.SerializerMethodField()
    cost_estimate = serializers.FloatField(read_only=True)
    created_at = serializers.SerializerMethodField()

    class Meta:
        model = QaRecord
        fields = [
            'id', 'question', 'answer', 'answer_type', 'root_type', 'rating',
            'latency_total_ms', 'tokens_prompt', 'tokens_completion',
            'cost_estimate', 'is_hit_cache', 'created_at',
        ]

    def get_rating(self, obj):
        # OneToOne LEFT JOIN：无反馈时 obj.feedback 为 None
        return obj.feedback.rating if (hasattr(obj, 'feedback') and obj.feedback) else 0

    def get_created_at(self, obj):
        return obj.created_at.isoformat()


class BadFeedbackSerializer(serializers.ModelSerializer):
    """差评反馈行序列化器

    替代 BadFeedbackListView 中手动构造 dict：
    - question/answer: 截断 100 字，qa_record 为空或字段为 None 时安全降级为空串
    - user: 优先 real_name，回退 username（user 为不可空 FK，判空仅为防御）
    - created_at: None 时返回空串（与手动实现一致）
    """

    question = serializers.SerializerMethodField()
    answer = serializers.SerializerMethodField()
    user = serializers.SerializerMethodField()
    created_at = serializers.SerializerMethodField()

    class Meta:
        model = QaFeedback
        fields = [
            'id', 'qa_record_id', 'question', 'answer', 'rating', 'tags',
            'comment', 'status', 'user', 'created_at',
        ]

    def get_question(self, obj):
        q = obj.qa_record
        return (q.question[:100] if q and q.question is not None else '')

    def get_answer(self, obj):
        q = obj.qa_record
        return (q.answer[:100] if q and q.answer is not None else '')

    def get_user(self, obj):
        return (obj.user.real_name or obj.user.username) if obj.user else ''

    def get_created_at(self, obj):
        return obj.created_at.isoformat() if obj.created_at else ''


class OrgUsageSerializer(serializers.ModelSerializer):
    """组织使用报表行序列化器

    替代 OrgUsageReportView 中手动构造 dict：
    - report_date: DateField 默认 ISO 输出（YYYY-MM-DD），与原 str(report_date) 一致
    - total_cost: DecimalField 转 float
    """

    total_cost = serializers.FloatField(read_only=True)

    class Meta:
        model = OrgUsageReport
        fields = [
            'id', 'report_date', 'department_id', 'department_name', 'team_id',
            'team_name', 'qa_count', 'user_count', 'total_tokens', 'total_cost',
            'avg_latency_ms', 'p95_latency_ms', 'good_feedback_rate',
            'cache_hit_count', 'cache_hit_rate',
        ]


class KeywordFeedbackAggSerializer(serializers.ModelSerializer):
    """关键词反馈聚合行序列化器

    替代 KeywordFeedbackAggListView 中 .values() 手动构造 dict：
    - applied_at/created_at: 用 SerializerMethodField 输出 UTC isoformat（可能为 None）
    - report_date: DateField 默认 ISO 输出，与原 JSON 渲染 date 对象一致
    """

    applied_at = serializers.SerializerMethodField()
    created_at = serializers.SerializerMethodField()

    class Meta:
        model = KeywordFeedbackAgg
        fields = [
            'id', 'report_date', 'keyword', 'root_type',
            'shown_count', 'click_count', 'adopt_count', 'bad_count',
            'click_rate', 'adopt_rate',
            'old_score', 'new_score', 'delta', 'reason',
            'adjust_type', 'status', 'applied_at', 'created_at',
        ]

    def get_applied_at(self, obj):
        return obj.applied_at.isoformat() if obj.applied_at else None

    def get_created_at(self, obj):
        return obj.created_at.isoformat() if obj.created_at else None


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
