"""retrieval urls"""
from django.urls import path
from apps.retrieval import views

urlpatterns = [
    path("search/", views.DebugSearchView.as_view()),
]
