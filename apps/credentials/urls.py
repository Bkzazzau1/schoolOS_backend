from django.urls import path

from .views import (
    ParentCredentialResetView,
    ParentPhoneChangeView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
    PublicRecoveryRequestView,
    SchoolRecoveryRequestDismissView,
    SchoolRecoveryRequestListView,
    StudentCredentialHandoffView,
    StudentCredentialResetView,
)

urlpatterns = [
    path("credentials/recovery/request/", PublicRecoveryRequestView.as_view()),
    path("credentials/password-reset/request/", PasswordResetRequestView.as_view()),
    path("credentials/password-reset/confirm/", PasswordResetConfirmView.as_view()),
    path(
        "schools/<uuid:school_id>/credentials/recovery/",
        SchoolRecoveryRequestListView.as_view(),
    ),
    path(
        "schools/<uuid:school_id>/credentials/recovery/<uuid:request_id>/dismiss/",
        SchoolRecoveryRequestDismissView.as_view(),
    ),
    path(
        "schools/<uuid:school_id>/students/<uuid:student_id>/credentials/",
        StudentCredentialHandoffView.as_view(),
    ),
    path(
        "schools/<uuid:school_id>/students/<uuid:student_id>/credentials/student/reset/",
        StudentCredentialResetView.as_view(),
    ),
    path(
        "schools/<uuid:school_id>/students/<uuid:student_id>/credentials/parent/reset/",
        ParentCredentialResetView.as_view(),
    ),
    path(
        "schools/<uuid:school_id>/students/<uuid:student_id>/credentials/parent/phone/",
        ParentPhoneChangeView.as_view(),
    ),
]
