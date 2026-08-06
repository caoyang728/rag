"""apps.wiki urls —— Wiki 页面浏览与管理路由"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.wiki import views

router = DefaultRouter()
router.register("pages", views.WikiPageViewSet, basename="wiki-page")

urlpatterns = [
    # 手动生成走独立视图，避免 /pages/generate/ 被 /pages/<pk>/ 拦截
    path("pages/generate/", views.WikiPageGenerateView.as_view()),
    path("", include(router.urls)),
]
