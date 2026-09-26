from django.urls import path

from . import views_families as fam
from . import views_fees as fees
from . import views_parent as parent

_BASE = "schools/<uuid:school_id>/receivables/"


def _actions(prefix, view, names, **kw):
    return [path(f"{_BASE}{prefix}/{name}/", view.as_view(action=name), **kw) for name in names]


urlpatterns = [
    # families
    path(f"{_BASE}families/", fam.FamiliesView.as_view()),
    path(f"{_BASE}families/bridge/", fam.BridgeView.as_view()),
    path(f"{_BASE}families/unassigned-students/", fam.StudentsWithoutFamilyView.as_view()),
    path(f"{_BASE}families/<uuid:family_id>/", fam.FamilyDetailView.as_view()),
    *[
        path(f"{_BASE}families/<uuid:family_id>/{name}/", fam.FamilyActionView.as_view(action=name))
        for name in ("rename", "add-student", "remove-student", "link-guardian", "set-status")
    ],
    path(f"{_BASE}families/<uuid:family_id>/receivables/", fam.FamilyReceivablesView.as_view()),
    path(f"{_BASE}families/<uuid:family_id>/statement/", fam.FamilyStatementView.as_view()),
    path(f"{_BASE}families/<uuid:family_id>/statements/", fam.FamilyStatementsView.as_view()),
    path(f"{_BASE}families/<uuid:family_id>/credit/", fam.FamilyCreditView.as_view()),
    path(f"{_BASE}families/<uuid:family_id>/credit/refund/", fam.FamilyRefundView.as_view()),
    path(f"{_BASE}families/<uuid:family_id>/payments/", fam.FamilyPaymentsView.as_view()),
    path(f"{_BASE}families/<uuid:family_id>/collection-accounts/", fam.FamilyAccountsView.as_view()),
    *[
        path(f"{_BASE}collection-accounts/<uuid:account_id>/{name}/", fam.CollectionAccountActionView.as_view(action=name))
        for name in ("suspend", "reinstate", "close", "mark-provisioned")
    ],
    # fee schedules
    path(f"{_BASE}fee-schedules/", fees.FeeSchedulesView.as_view()),
    path(f"{_BASE}fee-schedules/<uuid:schedule_id>/", fees.FeeScheduleDetailView.as_view()),
    path(f"{_BASE}fee-schedules/<uuid:schedule_id>/preview/", fees.FeeSchedulePreviewView.as_view()),
    *[
        path(f"{_BASE}fee-schedules/<uuid:schedule_id>/{name}/", fees.FeeScheduleActionView.as_view(action=name))
        for name in ("rename", "publish", "refresh", "retire", "clone", "void-charges")
    ],
    path(f"{_BASE}fee-schedules/<uuid:schedule_id>/items/", fees.FeeItemsView.as_view()),
    *[
        path(f"{_BASE}fee-schedules/<uuid:schedule_id>/items/<uuid:item_id>/{name}/", fees.FeeItemActionView.as_view(action=name))
        for name in ("update", "remove")
    ],
    # charges and the decisions about them
    path(f"{_BASE}charges/", fees.ReceivablesView.as_view()),
    path(f"{_BASE}charges/<uuid:receivable_id>/", fees.ReceivableDetailView.as_view()),
    *[
        path(f"{_BASE}charges/<uuid:receivable_id>/{name}/", fees.ReceivableActionView.as_view(action=name))
        for name in ("adjust", "void")
    ],
    path(f"{_BASE}adjustments/", fees.AdjustmentsView.as_view()),
    path(f"{_BASE}adjustments/<uuid:adjustment_id>/reverse/", fees.AdjustmentReverseView.as_view()),
    # payments
    path(f"{_BASE}payments/<uuid:transaction_id>/reallocate/", fees.ReallocateView.as_view()),
    # a parent's own view
    path(f"{_BASE}me/families/", parent.MyFamiliesView.as_view()),
    path(f"{_BASE}me/families/<uuid:family_id>/statement/", parent.MyFamilyStatementView.as_view()),
]
