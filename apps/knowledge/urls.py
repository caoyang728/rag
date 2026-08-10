"""knowledge urls"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from apps.knowledge import views

router = DefaultRouter()
router.register("nodes", views.KnowledgeNodeViewSet, basename="node")
router.register("documents", views.DocumentViewSet, basename="document")

urlpatterns = [
    path("nodes/tree/", views.NodeTreeView.as_view()),
    path("nodes/root_types/", views.RootTypesView.as_view()),
    path("documents/allowed_visibility/", views.AllowedVisibilityView.as_view()),
    path("documents/upload/", views.DocumentUploadView.as_view()),
    path("documents/<int:doc_id>/chunks/", views.DocumentChunksView.as_view()),
    path("documents/pending/", views.PendingDocsView.as_view()),
    # 文档审核：待审核列表 / 通过 / 驳回
    path("documents/pending-audits/", views.DocAuditPendingView.as_view()),
    path("documents/<int:pk>/audit-approve/", views.DocAuditApproveView.as_view()),
    path("documents/<int:pk>/audit-reject/", views.DocAuditRejectView.as_view()),
    path("celery/status/", views.CeleryStatusView.as_view()),
    path("queues/depth/", views.QueueDepthView.as_view()),
    # 注意：router.urls 必须放在最后，避免覆盖自定义路由
    path("", include(router.urls)),
]
