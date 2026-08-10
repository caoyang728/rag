"""agent urls"""
from django.urls import path
from apps.agent import views

urlpatterns = [
    path("task/plan/", views.AgentTaskPlanView.as_view()),
    path("task/run/", views.AgentTaskRunView.as_view()),
    path("workflows/", views.AgentWorkflowListView.as_view()),
    path("workflows/<int:workflow_id>/", views.AgentWorkflowDetailView.as_view()),
]
