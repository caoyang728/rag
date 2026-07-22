"""security urls"""
from django.urls import path
from apps.security import views

urlpatterns = [
    path("ip-whitelist/", views.IpWhitelistView.as_view()),
    path("ip-whitelist/<int:pk>/", views.IpWhitelistDetailView.as_view()),
    path("ip-blacklist/", views.IpBlacklistView.as_view()),
    path("ip-blacklist/<int:pk>/", views.IpBlacklistDetailView.as_view()),
    path("login-attempts/", views.LoginAttemptView.as_view()),
    path("sensitive-words/", views.SensitiveWordView.as_view()),
    path("sensitive-words/<int:pk>/", views.SensitiveWordDetailView.as_view()),
]