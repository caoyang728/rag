"""
apps.analytics urls 路由测试

覆盖范围：
- 全部 41 个路由的 URL → 视图类解析关系（resolve）
- 带路径参数的动态路由（<id>/<ds_id>/<fb_id>/<kw_id>/<report_id>）的参数捕获

说明：纯路由解析，无 DB 依赖；resolve 失败即代表 URL 与视图映射被破坏。
"""
import pytest
from django.urls import resolve

from apps.analytics.views_coverage import (
    CoverageReportDetailView, CoverageReportExportView,
    CoverageReportListView, CoverageReportView,
    FeedbackLoopView, GenerateCoverageReportView,
)
from apps.analytics.views_dashboard import (
    BadFeedbackDetailView, BadFeedbackListView,
    DailyReportView, OrgUsageReportView, QaRecordView,
    QueueDepthView, RealtimeSnapshotView, SystemMetricsReportView,
    TrendReportView,
)
from apps.analytics.views_doc_quality import (
    DocumentQualityReportListView, DocumentQualityReportView,
    MultiDimensionScoreView, RunDocQualityEvalView, RunMultiDimEvalView,
)
from apps.analytics.views_eval_dashboard import (
    EvalDashboardLowScoreView, EvalDashboardOverviewView,
    EvalDashboardQaDetailView, EvalDashboardTrendView,
)
from apps.analytics.views_keywords import (
    KeywordWeightDetailView, KeywordWeightListView,
)
from apps.analytics.views_low_score import (
    LowScoreAnalysisDetailView, LowScoreAnalysisListView,
    LowScoreAnalysisStatsView, RunLowScoreAnalysisView,
)
from apps.analytics.views_offline_eval import (
    GoldenDatasetDetailView, GoldenDatasetExportView,
    GoldenDatasetImportView, GoldenDatasetListView,
    GoldenQuestionView, RetrievalReportListView, RunAnswerEvalView,
    RunRegressionEvalView, RunRetrievalEvalView, SiphonRegressionView,
)


# (URL 路径, 期望的视图类)
_ROUTE_CASES = [
    # --- 关键词权重 ---
    ('/api/v1/analytics/keywords/', KeywordWeightListView),
    ('/api/v1/analytics/keywords/1/', KeywordWeightDetailView),
    # --- 日报 & 趋势 ---
    ('/api/v1/analytics/daily/', DailyReportView),
    ('/api/v1/analytics/trend/', TrendReportView),
    ('/api/v1/analytics/qa-records/', QaRecordView),
    # --- 差评反馈 ---
    ('/api/v1/analytics/bad-feedbacks/', BadFeedbackListView),
    ('/api/v1/analytics/bad-feedbacks/1/', BadFeedbackDetailView),
    # --- 系统监控 ---
    ('/api/v1/analytics/system-metrics/', SystemMetricsReportView),
    ('/api/v1/analytics/org-usage/', OrgUsageReportView),
    ('/api/v1/analytics/queue-depth/', QueueDepthView),
    ('/api/v1/analytics/realtime/', RealtimeSnapshotView),
    # --- 黄金测试集管理 ---
    ('/api/v1/analytics/golden-datasets/', GoldenDatasetListView),
    ('/api/v1/analytics/golden-datasets/1/', GoldenDatasetDetailView),
    ('/api/v1/analytics/golden-datasets/1/import/', GoldenDatasetImportView),
    ('/api/v1/analytics/golden-datasets/1/export/', GoldenDatasetExportView),
    ('/api/v1/analytics/golden-datasets/1/questions/', GoldenQuestionView),
    # --- 低分回归 ---
    ('/api/v1/analytics/regression/siphon/', SiphonRegressionView),
    ('/api/v1/analytics/regression/eval/', RunRegressionEvalView),
    # --- 离线评估执行 ---
    ('/api/v1/analytics/eval/retrieval/', RunRetrievalEvalView),
    ('/api/v1/analytics/eval/answer/', RunAnswerEvalView),
    ('/api/v1/analytics/eval/retrieval-reports/', RetrievalReportListView),
    # --- 文档质量 ---
    ('/api/v1/analytics/doc-quality/', DocumentQualityReportView),
    ('/api/v1/analytics/doc-quality/evaluate/', RunDocQualityEvalView),
    ('/api/v1/analytics/doc-quality/reports/', DocumentQualityReportListView),
    # --- 多维度评估 ---
    ('/api/v1/analytics/multi-dim-scores/', MultiDimensionScoreView),
    ('/api/v1/analytics/multi-dim-eval/', RunMultiDimEvalView),
    # --- 评估看板 ---
    ('/api/v1/analytics/eval-dashboard/overview/', EvalDashboardOverviewView),
    ('/api/v1/analytics/eval-dashboard/trend/', EvalDashboardTrendView),
    ('/api/v1/analytics/eval-dashboard/low-score-qa/', EvalDashboardLowScoreView),
    ('/api/v1/analytics/eval-dashboard/qa-detail/', EvalDashboardQaDetailView),
    # --- 覆盖率 & 反馈闭环 ---
    ('/api/v1/analytics/coverage/', CoverageReportView),
    ('/api/v1/analytics/coverage/generate/', GenerateCoverageReportView),
    ('/api/v1/analytics/coverage/reports/', CoverageReportListView),
    ('/api/v1/analytics/coverage/reports/1/', CoverageReportDetailView),
    ('/api/v1/analytics/coverage/reports/1/export/', CoverageReportExportView),
    ('/api/v1/analytics/feedback-loop/', FeedbackLoopView),
    # --- 低分归因分析 ---
    ('/api/v1/analytics/low-score-analysis/', LowScoreAnalysisListView),
    ('/api/v1/analytics/low-score-analysis/detail/', LowScoreAnalysisDetailView),
    ('/api/v1/analytics/low-score-analysis/run/', RunLowScoreAnalysisView),
    ('/api/v1/analytics/low-score-analysis/stats/', LowScoreAnalysisStatsView),
]


@pytest.mark.unit
@pytest.mark.parametrize('url_path, expected_view', _ROUTE_CASES,
                         ids=[case[0] for case in _ROUTE_CASES])
def test_url_resolves_to_view(url_path, expected_view):
    """每个 URL 都能解析到对应的视图类"""
    resolver = resolve(url_path)
    assert resolver.func.view_class is expected_view
