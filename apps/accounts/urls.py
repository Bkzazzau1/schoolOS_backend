from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import (
    EmailVerificationConfirmView,
    EmailVerificationSendView,
    ProprietorRegisterView,
)

urlpatterns = [
    path("register/", ProprietorRegisterView.as_view()),
    path("email-verification/send/", EmailVerificationSendView.as_view()),
    path("email-verification/confirm/", EmailVerificationConfirmView.as_view()),
    path("token/", TokenObtainPairView.as_view()),
    path("token/refresh/", TokenRefreshView.as_view()),
]
