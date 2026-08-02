"""users urls"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from apps.users import views

router = DefaultRouter()
router.register("users", views.UserViewSet, basename="user")
router.register("departments", views.DepartmentViewSet, basename="department")
router.register("teams", views.TeamViewSet, basename="team")
router.register("roles", views.RoleViewSet, basename="role")
router.register("permissions", views.PermissionViewSet, basename="permission")

urlpatterns = [
    path("login/", views.LoginView.as_view()),
    path("logout/", views.LogoutView.as_view()),
    path("profile/", views.ProfileView.as_view()),
    path("reset-password/", views.ResetPasswordView.as_view()),
    path("password-reset/request/", views.PasswordResetRequestView.as_view()),
    path("password-reset/confirm/", views.PasswordResetConfirmView.as_view()),
    path("token/refresh/", TokenRefreshView.as_view()),
    # 个人权限查看 / 权限申请
    path("permissions/me/", views.MyPermissionsView.as_view()),
    path("permissions/approvers/", views.PermissionApproversView.as_view()),
    path("permissions/applications/", views.AccessApplicationView.as_view()),
    path("permissions/applications/<int:pk>/withdraw/", views.AccessApplicationWithdrawView.as_view()),
    # 可申请角色清单 + 审批链预览(申请前展示用)
    path("permissions/assignable-roles/", views.AssignableRolesView.as_view()),
    path("permissions/approval-chain-preview/", views.ApprovalChainPreviewView.as_view()),
    # 工单审批池：待我审批 / 我发起的工单
    path("permissions/pending-approvals/", views.PendingApprovalTicketsView.as_view()),
    path("permissions/my-tickets/", views.MyTicketsView.as_view()),
    # 审批操作：通过 / 驳回
    path("permissions/tickets/<int:pk>/approve/", views.TicketApproveView.as_view()),
    path("permissions/tickets/<int:pk>/reject/", views.TicketRejectView.as_view()),
    path("", include(router.urls)),
]
