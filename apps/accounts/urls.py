from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    EmailVerificationConfirmView,
    EmailVerificationSendView,
    InitialPasswordChangeView,
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
]
