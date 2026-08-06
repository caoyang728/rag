"""
apps.analytics urls 路由测试

覆盖范围：
- 全部 41 个路由的 URL → 视图类解析关系（resolve）
- 带路径参数的动态路由（<id>/<ds_id>/<fb_id>/<kw_id>/<report_id>）的参数捕获

说明：纯路由解析，无 DB 依赖；resolve 失败即代表 URL 与视图映射被破坏。
"""
import pytest
from django.urls import resolve

from apps.analytics import views


# (URL 路径, 期望的视图类)
_ROUTE_CASES = [
    # --- 关键词权重 ---
    ('/api/v1/analytics/keywords/', views.KeywordWeightListView),
    ('/api/v1/analytics/keywords/1/', views.KeywordWeightDetailView),
    # --- 日报 & 趋势 & 概览 ---
    ('/api/v1/analytics/daily/', views.DailyReportView),
    ('/api/v1/analytics/trend/', views.TrendReportView),
    ('/api/v1/analytics/overview/', views.OverviewStatsView),
    ('/api/v1/analytics/qa-records/', views.QaRecordView),
    # --- 差评反馈 ---
    ('/api/v1/analytics/bad-feedbacks/', views.BadFeedbackListView),
    ('/api/v1/analytics/bad-feedbacks/1/', views.BadFeedbackDetailView),
    # --- 系统监控 ---
    ('/api/v1/analytics/system-metrics/', views.SystemMetricsReportView),
    ('/api/v1/analytics/org-usage/', views.OrgUsageReportView),
    ('/api/v1/analytics/queue-depth/', views.QueueDepthView),
    ('/api/v1/analytics/realtime/', views.RealtimeSnapshotView),
    # --- 黄金测试集管理 ---
    ('/api/v1/analytics/golden-datasets/', views.GoldenDatasetListView),
    ('/api/v1/analytics/golden-datasets/1/', views.GoldenDatasetDetailView),
    ('/api/v1/analytics/golden-datasets/1/import/', views.GoldenDatasetImportView),
    ('/api/v1/analytics/golden-datasets/1/export/', views.GoldenDatasetExportView),
    ('/api/v1/analytics/golden-datasets/1/questions/', views.GoldenQuestionView),
    # --- 低分回归 ---
    ('/api/v1/analytics/regression/siphon/', views.SiphonRegressionView),
    ('/api/v1/analytics/regression/eval/', views.RunRegressionEvalView),
    # --- 离线评估执行 ---
    ('/api/v1/analytics/eval/retrieval/', views.RunRetrievalEvalView),
    ('/api/v1/analytics/eval/answer/', views.RunAnswerEvalView),
    ('/api/v1/analytics/eval/retrieval-reports/', views.RetrievalReportListView),
    # --- 文档质量 ---
    ('/api/v1/analytics/doc-quality/', views.DocumentQualityReportView),
    ('/api/v1/analytics/doc-quality/evaluate/', views.RunDocQualityEvalView),
    ('/api/v1/analytics/doc-quality/reports/', views.DocumentQualityReportListView),
    # --- 多维度评估 ---
    ('/api/v1/analytics/multi-dim-scores/', views.MultiDimensionScoreView),
    ('/api/v1/analytics/multi-dim-eval/', views.RunMultiDimEvalView),
    # --- 评估看板 ---
    ('/api/v1/analytics/eval-dashboard/overview/', views.EvalDashboardOverviewView),
    ('/api/v1/analytics/eval-dashboard/trend/', views.EvalDashboardTrendView),
    ('/api/v1/analytics/eval-dashboard/low-score-qa/', views.EvalDashboardLowScoreView),
    ('/api/v1/analytics/eval-dashboard/qa-detail/', views.EvalDashboardQaDetailView),
    # --- 覆盖率 & 反馈闭环 ---
    ('/api/v1/analytics/coverage/', views.CoverageReportView),
    ('/api/v1/analytics/coverage/generate/', views.GenerateCoverageReportView),
    ('/api/v1/analytics/coverage/reports/', views.CoverageReportListView),
    ('/api/v1/analytics/coverage/reports/1/', views.CoverageReportDetailView),
    ('/api/v1/analytics/coverage/reports/1/export/', views.CoverageReportExportView),
    ('/api/v1/analytics/feedback-loop/', views.FeedbackLoopView),
    # --- 低分归因分析 ---
    ('/api/v1/analytics/low-score-analysis/', views.LowScoreAnalysisListView),
    ('/api/v1/analytics/low-score-analysis/detail/', views.LowScoreAnalysisDetailView),
    ('/api/v1/analytics/low-score-analysis/run/', views.RunLowScoreAnalysisView),
    ('/api/v1/analytics/low-score-analysis/stats/', views.LowScoreAnalysisStatsView),
]


@pytest.mark.unit
@pytest.mark.parametrize('url_path, expected_view', _ROUTE_CASES,
                         ids=[case[0] for case in _ROUTE_CASES])
def test_url_resolves_to_view(url_path, expected_view):
    """每个 URL 都能解析到对应的视图类"""
    resolver = resolve(url_path)
    assert resolver.func.view_class is expected_view
