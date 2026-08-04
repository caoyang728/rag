"""system urls"""
from django.urls import path
from apps.system import views

urlpatterns = [
    path("health/", views.HealthView.as_view()),
    path("configs/", views.SystemConfigView.as_view()),
    path("configs/<str:key>/", views.SystemConfigView.as_view()),
    # 模型管理 CRUD：显式 as_view 映射，避免引入 DRF router 与现有 path 风格不一致
    path("llm-models/", views.LLMModelViewSet.as_view({'get': 'list', 'post': 'create'})),
    path("llm-models/<int:pk>/", views.LLMModelViewSet.as_view({
        'get': 'retrieve', 'put': 'update', 'patch': 'partial_update', 'delete': 'destroy'})),
    # 配置变更工单：创建/列表/详情/审批/驳回/撤回
    # create_ticket 是工单提交主入口（POST），与 PUT /configs/<key>/ 行为一致
    path("config-tickets/", views.ConfigChangeTicketViewSet.as_view({
        'get': 'list', 'post': 'create_ticket'})),
    path("config-tickets/<int:pk>/", views.ConfigChangeTicketViewSet.as_view({'get': 'retrieve'})),
    path("config-tickets/<int:pk>/approve/", views.ConfigChangeTicketViewSet.as_view({'post': 'approve'})),
    path("config-tickets/<int:pk>/reject/", views.ConfigChangeTicketViewSet.as_view({'post': 'reject'})),
    path("config-tickets/<int:pk>/withdraw/", views.ConfigChangeTicketViewSet.as_view({'post': 'withdraw'})),
    # 模型变更工单：列表/详情/审批/驳回/撤回
    path("model-tickets/", views.ModelChangeTicketViewSet.as_view({'get': 'list'})),
    path("model-tickets/<int:pk>/", views.ModelChangeTicketViewSet.as_view({'get': 'retrieve'})),
    path("model-tickets/<int:pk>/approve/", views.ModelChangeTicketViewSet.as_view({'post': 'approve'})),
    path("model-tickets/<int:pk>/reject/", views.ModelChangeTicketViewSet.as_view({'post': 'reject'})),
    path("model-tickets/<int:pk>/withdraw/", views.ModelChangeTicketViewSet.as_view({'post': 'withdraw'})),
    path("stats/", views.StatsView.as_view()),
    path("search/", views.GlobalSearchView.as_view()),
]
