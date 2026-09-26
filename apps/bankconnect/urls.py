from django.urls import path

from .transaction_views import (
    DecideView,
    ReviewQueueView,
    StudentSearchView,
    SummaryView,
    TransactionDetailView,
    TransactionsView,
)
from .views import (
    ACTIONS,
    BankWebhookView,
    ConnectionActionView,
    ConnectionAuditView,
    ConnectionsView,
    ConnectionWebhookView,
    ProvidersView,
)

_BASE = "schools/<uuid:school_id>/collections/"

urlpatterns = [
    path(f"{_BASE}providers/", ProvidersView.as_view()),
    path(f"{_BASE}connections/", ConnectionsView.as_view()),
    path(f"{_BASE}connections/<uuid:connection_id>/audit/", ConnectionAuditView.as_view()),
    path(f"{_BASE}connections/<uuid:connection_id>/webhook/", ConnectionWebhookView.as_view()),
    path(f"{_BASE}transactions/", TransactionsView.as_view()),
    path(f"{_BASE}transactions/<uuid:transaction_id>/", TransactionDetailView.as_view()),
    path(f"{_BASE}transactions/<uuid:transaction_id>/decide/", DecideView.as_view()),
    path(f"{_BASE}review/", ReviewQueueView.as_view()),
    path(f"{_BASE}summary/", SummaryView.as_view()),
    path(f"{_BASE}students/", StudentSearchView.as_view()),
    path("bank-webhooks/<slug:provider>/<str:token>/", BankWebhookView.as_view()),
    *[
        path(f"{_BASE}connections/<uuid:connection_id>/{name}/", ConnectionActionView.as_view(action=name))
        for name in ACTIONS
    ],
]
