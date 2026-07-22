"""agent urls"""
from django.urls import path
from apps.agent import views

urlpatterns = [
    path("task/plan/", views.AgentTaskPlanView.as_view()),
    path("task/run/", views.AgentTaskRunView.as_view()),
]
