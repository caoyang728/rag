"""notification urls"""
from django.urls import path
from apps.notification import views

urlpatterns = [
    path("subscriptions/", views.SubscriptionView.as_view()),
    path("send-logs/", views.SendLogView.as_view()),
]
