from django.urls import path

from . import views as v

_BASE = "schools/<uuid:school_id>/collections/"

BATCH_ACTIONS = ("preview", "selection", "title", "policy", "submit", "approve", "reject", "cancel", "start", "retry")
ITEM_ACTIONS = ("override", "override-clear", "arrears")

urlpatterns = [
    path(f"{_BASE}dashboard/", v.DashboardView.as_view()),
    path(f"{_BASE}policy/", v.PolicyView.as_view()),
    path(f"{_BASE}policy/effective/", v.PolicyEffectiveView.as_view()),
    path(f"{_BASE}policy/overrides/", v.PolicyOverridesView.as_view()),
    path(f"{_BASE}policy/overrides/<uuid:override_id>/remove/", v.PolicyOverrideRemoveView.as_view()),
    path(f"{_BASE}switches/", v.SwitchesView.as_view()),
    path(f"{_BASE}switches/<uuid:switch_id>/", v.SwitchDetailView.as_view()),
    *[path(f"{_BASE}switches/<uuid:switch_id>/{name}/", v.SwitchActionView.as_view(action=name)) for name in ("apply", "cancel")],
    path(f"{_BASE}batches/", v.BatchesView.as_view()),
    path(f"{_BASE}batches/<uuid:batch_id>/", v.BatchDetailView.as_view()),
    path(f"{_BASE}batches/<uuid:batch_id>/items/", v.BatchItemsView.as_view()),
    path(f"{_BASE}batches/<uuid:batch_id>/events/", v.BatchEventsView.as_view()),
    path(f"{_BASE}batches/<uuid:batch_id>/export/", v.BatchExportView.as_view()),
    path(f"{_BASE}batches/<uuid:batch_id>/progress/", v.BatchProgressView.as_view()),
    path(f"{_BASE}batches/<uuid:batch_id>/failed/", v.BatchFailedView.as_view()),
    *[path(f"{_BASE}batches/<uuid:batch_id>/{name}/", v.BatchActionView.as_view(action=name)) for name in BATCH_ACTIONS],
    *[path(f"{_BASE}batches/<uuid:batch_id>/items/<uuid:item_id>/{name}/", v.BatchItemActionView.as_view(action=name)) for name in ITEM_ACTIONS],
    path(f"{_BASE}families/<uuid:family_id>/payer-identity/", v.PayerIdentityView.as_view()),
]
