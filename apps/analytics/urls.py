"""analytics urls"""
from django.urls import path
from apps.analytics import views

urlpatterns = [
    path("keywords/", views.KeywordWeightListView.as_view()),
    path("keywords/<int:kw_id>/", views.KeywordWeightDetailView.as_view()),
    path("daily/", views.DailyReportView.as_view()),
    path("trend/", views.TrendReportView.as_view()),
    path("bad-feedbacks/", views.BadFeedbackListView.as_view()),
    path("bad-feedbacks/<int:fb_id>/", views.BadFeedbackDetailView.as_view()),
    path("overview/", views.OverviewStatsView.as_view()),
    path("qa-records/", views.QaRecordView.as_view()),
]