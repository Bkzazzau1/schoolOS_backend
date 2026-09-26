from django.urls import path

from .views import (
    ACTIONS,
    AuthorizationStartView,
    BankWebhookView,
    ConnectionActionView,
    ConnectionAuditView,
    ConnectionsView,
    ConnectionSyncView,
    ProvidersView,
    TransactionsView,
)

_BASE = "schools/<uuid:school_id>/collections/"

urlpatterns = [
    path(f"{_BASE}providers/", ProvidersView.as_view()),
    path(f"{_BASE}connections/", ConnectionsView.as_view()),
    path(f"{_BASE}connections/authorize/", AuthorizationStartView.as_view()),
    path(f"{_BASE}connections/<uuid:connection_id>/audit/", ConnectionAuditView.as_view()),
    path(f"{_BASE}connections/<uuid:connection_id>/sync/", ConnectionSyncView.as_view()),
    path(f"{_BASE}transactions/", TransactionsView.as_view()),
    path("bank-webhooks/<slug:provider>/<str:token>/", BankWebhookView.as_view()),
    *[
        path(f"{_BASE}connections/<uuid:connection_id>/{name}/", ConnectionActionView.as_view(action=name))
        for name in ACTIONS
    ],
]
