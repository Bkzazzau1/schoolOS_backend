from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    DeviceTokenRegisterView,
    DeviceTokenUnregisterView,
    EmailVerificationConfirmView,
    EmailVerificationSendView,
    InitialPasswordChangeView,
    LogoutView,
    ProprietorRegisterView,
    SchoolOSTokenView,
)

urlpatterns = [
    path("register/", ProprietorRegisterView.as_view()),
    path("email-verification/send/", EmailVerificationSendView.as_view()),
    path("email-verification/confirm/", EmailVerificationConfirmView.as_view()),
    path("password/initial-change/", InitialPasswordChangeView.as_view()),
    path("token/", SchoolOSTokenView.as_view()),
    path("token/refresh/", TokenRefreshView.as_view()),
    path("logout/", LogoutView.as_view()),
    path("device-tokens/", DeviceTokenRegisterView.as_view()),
    path("device-tokens/unregister/", DeviceTokenUnregisterView.as_view()),
]
