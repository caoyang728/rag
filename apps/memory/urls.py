"""memory urls"""
from django.urls import path
from apps.memory import views

urlpatterns = [
    path("context/", views.MemoryDebugView.as_view()),
    path("refine/", views.RefineMemoryView.as_view()),
    path("user-memory/", views.UserMemoryView.as_view()),
]
