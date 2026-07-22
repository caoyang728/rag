"""system urls"""
from django.urls import path
from apps.system import views

urlpatterns = [
    path("health/", views.HealthView.as_view()),
    path("configs/", views.SystemConfigView.as_view()),
    path("configs/<str:key>/", views.SystemConfigView.as_view()),
    path("stats/", views.StatsView.as_view()),
    path("search/", views.GlobalSearchView.as_view()),
]
