"""users urls"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from apps.users.views_auth import (
    LoginView, LogoutView, ProfileView,
    ResetPasswordView, PasswordResetRequestView, PasswordResetConfirmView,
)
from apps.users.views_users import UserViewSet
from apps.users.views_org import DepartmentViewSet, TeamViewSet
from apps.users.views_rbac import RoleViewSet, PermissionViewSet
from apps.users.views_permissions import (
    MyPermissionsView, PermissionApproversView,
    AccessApplicationView, AccessApplicationWithdrawView,
    AssignableRolesView, ApprovalChainPreviewView,
)
from apps.users.views_tickets import (
    TicketApproveView, TicketRejectView,
    TicketCenterView, TicketCenterApproveView, TicketCenterRejectView, TicketCenterWithdrawView,
)

router = DefaultRouter()
router.register("users", UserViewSet, basename="user")
router.register("departments", DepartmentViewSet, basename="department")
router.register("teams", TeamViewSet, basename="team")
router.register("roles", RoleViewSet, basename="role")
router.register("permissions", PermissionViewSet, basename="permission")

urlpatterns = [
    path("login/", LoginView.as_view()),
    path("logout/", LogoutView.as_view()),
    path("profile/", ProfileView.as_view()),
    path("reset-password/", ResetPasswordView.as_view()),
    path("password-reset/request/", PasswordResetRequestView.as_view()),
    path("password-reset/confirm/", PasswordResetConfirmView.as_view()),
    path("token/refresh/", TokenRefreshView.as_view()),
    # 个人权限查看 / 权限申请
    path("permissions/me/", MyPermissionsView.as_view()),
    path("permissions/approvers/", PermissionApproversView.as_view()),
    path("permissions/applications/", AccessApplicationView.as_view()),
    path("permissions/applications/<int:pk>/withdraw/", AccessApplicationWithdrawView.as_view()),
    # 可申请角色清单 + 审批链预览(申请前展示用)
    path("permissions/assignable-roles/", AssignableRolesView.as_view()),
    path("permissions/approval-chain-preview/", ApprovalChainPreviewView.as_view()),
    # 审批操作：通过 / 驳回（permission 域审批，工单中心按类型委托）
    path("permissions/tickets/<int:pk>/approve/", TicketApproveView.as_view()),
    path("permissions/tickets/<int:pk>/reject/", TicketRejectView.as_view()),
    # 统一工单中心：全部类型工单一页展示（权限/配置/定时/模型）+ 统一审批操作
    path("tickets/", TicketCenterView.as_view()),
    path("tickets/<int:pk>/approve/", TicketCenterApproveView.as_view()),
    path("tickets/<int:pk>/reject/", TicketCenterRejectView.as_view()),
    path("tickets/<int:pk>/withdraw/", TicketCenterWithdrawView.as_view()),
    path("", include(router.urls)),
]
