"""chat urls"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from apps.chat import views

router = DefaultRouter()
router.register("sessions", views.SessionViewSet, basename="session")

urlpatterns = [
    # 同步问答接口 ChatAskView 已软删除，前端全部走 ask_stream SSE 流式
    path("config/", views.ChatConfigView.as_view()),
    path("ask_stream/", views.ChatAskStreamView.as_view()),
    path("feedback/", views.FeedbackView.as_view()),
    path("records/", views.QaRecordListView.as_view()),
    path("records/<int:pk>/", views.QaRecordDeleteView.as_view()),
    path("", include(router.urls)),
]
