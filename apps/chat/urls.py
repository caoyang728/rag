"""chat urls"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from apps.chat import views

router = DefaultRouter()
router.register("sessions", views.SessionViewSet, basename="session")

urlpatterns = [
    path("ask/", views.ChatAskView.as_view()),
    path("feedback/", views.FeedbackView.as_view()),
    path("records/", views.QaRecordListView.as_view()),
    path("", include(router.urls)),
]
