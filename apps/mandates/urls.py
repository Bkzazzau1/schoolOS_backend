from django.urls import path

from . import views_debits as d
from . import views_mandates as m
from . import views_providers as p

_BASE = "schools/<uuid:school_id>/mandates/"

CONNECTION_ACTIONS = ("test", "rename", "disable", "enable", "replace-credentials", "disconnect", "webhook-token")
MANDATE_ACTIONS = ("refresh", "retry-setup", "resend-activation", "cancel", "suspend", "reactivate", "primary")
PAYER_ACTIONS = ("consent", "activation-request", "activation-confirm", "refresh", "cancel")
BATCH_ACTIONS = ("preview", "selection", "title", "submit", "approve", "reject", "cancel", "start")

urlpatterns = [
    path(f"{_BASE}overview/", d.OverviewView.as_view()),
    path(f"{_BASE}providers/", p.ProvidersView.as_view()),
    path(f"{_BASE}connections/", p.ConnectionsView.as_view()),
    path(f"{_BASE}connections/<uuid:connection_id>/audit/", p.ConnectionAuditView.as_view()),
    path(f"{_BASE}connections/<uuid:connection_id>/webhook/", p.ConnectionWebhookView.as_view()),
    path(f"{_BASE}connections/<uuid:connection_id>/banks/", p.ConnectionBanksView.as_view()),
    *[path(f"{_BASE}connections/<uuid:connection_id>/{name}/", p.ConnectionActionView.as_view(action=name)) for name in CONNECTION_ACTIONS],
    path(f"{_BASE}families/<uuid:family_id>/payers/", m.FamilyPayersView.as_view()),
    path(f"{_BASE}mandates/", m.MandatesView.as_view()),
    path(f"{_BASE}mandates/<uuid:mandate_id>/", m.MandateDetailView.as_view()),
    *[path(f"{_BASE}mandates/<uuid:mandate_id>/{name}/", m.MandateActionView.as_view(action=name)) for name in MANDATE_ACTIONS],
    path(f"{_BASE}my-mandates/", m.PayerMandatesView.as_view()),
    path(f"{_BASE}my-mandates/<uuid:mandate_id>/", m.PayerMandateDetailView.as_view()),
    *[path(f"{_BASE}my-mandates/<uuid:mandate_id>/{name}/", m.PayerMandateActionView.as_view(action=name)) for name in PAYER_ACTIONS],
    path(f"{_BASE}debit-batches/", d.BatchesView.as_view()),
    path(f"{_BASE}debit-batches/<uuid:batch_id>/", d.BatchDetailView.as_view()),
    path(f"{_BASE}debit-batches/<uuid:batch_id>/items/", d.BatchItemsView.as_view()),
    path(f"{_BASE}debit-batches/<uuid:batch_id>/events/", d.BatchEventsView.as_view()),
    path(f"{_BASE}debit-batches/<uuid:batch_id>/progress/", d.BatchProgressView.as_view()),
    path(f"{_BASE}debit-batches/<uuid:batch_id>/failed/", d.BatchFailedView.as_view()),
    path(f"{_BASE}debit-batches/<uuid:batch_id>/retry/", d.BatchRetryView.as_view()),
    path(f"{_BASE}debit-batches/<uuid:batch_id>/items/<uuid:item_id>/amount/", d.ItemAmountView.as_view()),
    *[path(f"{_BASE}debit-batches/<uuid:batch_id>/{name}/", d.BatchActionView.as_view(action=name)) for name in BATCH_ACTIONS],
    path(f"{_BASE}transactions/", d.TransactionsView.as_view()),
    path(f"{_BASE}transactions/<uuid:transaction_id>/check/", d.TransactionCheckView.as_view()),
    path("mandate-webhooks/<str:provider>/<str:token>/", p.MandateWebhookView.as_view()),
]
