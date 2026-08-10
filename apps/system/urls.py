"""system urls"""
from django.urls import path
from apps.system import views

urlpatterns = [
    path("health/", views.HealthView.as_view()),
    path("configs/", views.SystemConfigView.as_view()),
    path("configs/<str:key>/", views.SystemConfigView.as_view()),
    # 定时任务调度配置列表（修改走 configs/<key>/ 工单审批流程）
    path("scheduler/tasks/", views.SchedulerTaskView.as_view()),
    # 模型管理 CRUD：显式 as_view 映射，避免引入 DRF router 与现有 path 风格不一致
    path("llm-models/", views.LLMModelViewSet.as_view({'get': 'list', 'post': 'create'})),
    path("llm-models/<int:pk>/", views.LLMModelViewSet.as_view({
        'get': 'retrieve', 'put': 'update', 'patch': 'partial_update', 'delete': 'destroy'})),
    # 统一工单列表+创建+操作：合并配置变更/定时任务/模型变更工单
    path("tickets/", views.TicketViewSet.as_view(), name='ticket-list-create'),
    path("tickets/<int:pk>/", views.TicketViewSet.as_view(), name='ticket-detail'),
    path("tickets/<int:pk>/approve/", views.ApproveTicketView.as_view(), name='ticket-approve'),
    path("tickets/<int:pk>/reject/", views.RejectTicketView.as_view(), name='ticket-reject'),
    path("tickets/<int:pk>/withdraw/", views.WithdrawTicketView.as_view(), name='ticket-withdraw'),
    path("stats/", views.StatsView.as_view()),
    # 后台任务看板：任务日志列表 / 状态统计 + 队列深度 / 失败重试
    path("tasks/", views.TaskLogView.as_view()),
    path("tasks/stats/", views.TaskStatsView.as_view()),
    path("tasks/<str:task_id>/retry/", views.TaskRetryView.as_view()),
]
