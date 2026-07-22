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
    path("token/refresh/", TokenRefreshView.as_view()),
    # 个人权限查看 / 权限申请
    path("permissions/me/", views.MyPermissionsView.as_view()),
    path("permissions/approvers/", views.PermissionApproversView.as_view()),
    path("permissions/applications/", views.PermissionApplicationView.as_view()),
    path("permissions/applications/<int:pk>/withdraw/", views.PermissionApplicationWithdrawView.as_view()),
    path("", include(router.urls)),
]
