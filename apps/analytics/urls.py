"""analytics urls - 包含 RAG 质量评估全套接口"""
from django.urls import path
from apps.analytics import views

urlpatterns = [
    # --- 关键词权重---
    path("keywords/", views.KeywordWeightListView.as_view()),
    path("keywords/<int:kw_id>/", views.KeywordWeightDetailView.as_view()),

    # --- 日报 & 趋势---
    path("daily/", views.DailyReportView.as_view()),
    path("trend/", views.TrendReportView.as_view()),
    path("overview/", views.OverviewStatsView.as_view()),
    path("qa-records/", views.QaRecordView.as_view()),

    # --- 差评反馈---
    path("bad-feedbacks/", views.BadFeedbackListView.as_view()),
    path("bad-feedbacks/<int:fb_id>/", views.BadFeedbackDetailView.as_view()),

    # --- 系统监控---
    path("system-metrics/", views.SystemMetricsReportView.as_view()),
    path("org-usage/", views.OrgUsageReportView.as_view()),
    path("queue-depth/", views.QueueDepthView.as_view()),
    path("realtime/", views.RealtimeSnapshotView.as_view()),
    path("quality-reports/", views.QualityReportView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 黄金测试集管理
    # ========================================================================
    path("golden-datasets/", views.GoldenDatasetListView.as_view()),
    path("golden-datasets/<int:ds_id>/", views.GoldenDatasetDetailView.as_view()),
    path("golden-datasets/<int:ds_id>/import/", views.GoldenDatasetImportView.as_view()),
    path("golden-datasets/<int:ds_id>/export/", views.GoldenDatasetExportView.as_view()),
    path("golden-datasets/<int:ds_id>/questions/", views.GoldenQuestionView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 离线评估执行
    # ========================================================================
    path("eval/retrieval/", views.RunRetrievalEvalView.as_view()),
    path("eval/answer/", views.RunAnswerEvalView.as_view()),
    path("eval/retrieval-reports/", views.RetrievalReportListView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 文档质量
    # ========================================================================
    path("doc-quality/", views.DocumentQualityReportView.as_view()),
    path("doc-quality/evaluate/", views.RunDocQualityEvalView.as_view()),
    path("doc-quality/reports/", views.DocumentQualityReportListView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 多维度评估
    # ========================================================================
    path("multi-dim-scores/", views.MultiDimensionScoreView.as_view()),
    path("multi-dim-eval/", views.RunMultiDimEvalView.as_view()),

    # ========================================================================
    # RAG 质量评估 - 覆盖率 & 反馈闭环
    # ========================================================================
    path("coverage/", views.CoverageReportView.as_view()),
    path("coverage/generate/", views.GenerateCoverageReportView.as_view()),
    path("coverage/reports/", views.CoverageReportListView.as_view()),
    path("coverage/reports/<int:report_id>/", views.CoverageReportDetailView.as_view()),
    path("coverage/reports/<int:report_id>/export/", views.CoverageReportExportView.as_view()),
    path("feedback-loop/", views.FeedbackLoopView.as_view()),
]