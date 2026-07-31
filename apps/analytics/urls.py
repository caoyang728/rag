"""analytics urls"""
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
]