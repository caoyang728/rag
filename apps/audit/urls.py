"""audit urls"""
from django.urls import path
from apps.audit import views

urlpatterns = [
    path("logs/", views.AuditLogListView.as_view()),
    path("verify-chain/", views.VerifyChainView.as_view()),
]
