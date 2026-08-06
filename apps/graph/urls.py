"""apps.graph urls —— 图谱可视化与实体检索路由"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.graph import views

router = DefaultRouter()
router.register("entities", views.EntityViewSet, basename="graph-entity")
router.register("communities", views.CommunityViewSet, basename="graph-community")

urlpatterns = [
    path("", include(router.urls)),
]
