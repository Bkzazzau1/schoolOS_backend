"""The automatic SkulGen billing-cycle engine, split into one file per responsibility (status
escalation, metering/automation display, invoice issuance, the org-wide shared-period flavor,
the per-school term flavor, and the runner that ties them together) because a single module
grew past apps/core/tests/test_code_size.py's 700-line limit. This package's own public name is
unchanged - every existing `from apps.billing.cycle import ...` caller, and the handful of
tests that reach a couple of internal `_locked` helpers directly, keep working unchanged.
"""

from ..exceptions import BillingCycleNotReady
from .invoicing import _create_school_invoice_locked, _issue_period_invoice_locked
from .metering import _policy_for, automation_summary, publish_school_billing_meter
from .runner import run_billing_cycles, run_subscription_cycle
from .status import BILLING_CONTINUES_STATUSES
from .term_billing import _issue_term_invoice_for_school_locked, _run_term_billing_cycle

__all__ = [
    "BillingCycleNotReady",
    "BILLING_CONTINUES_STATUSES",
    "automation_summary",
    "publish_school_billing_meter",
    "run_billing_cycles",
    "run_subscription_cycle",
    "_create_school_invoice_locked",
    "_issue_period_invoice_locked",
    "_issue_term_invoice_for_school_locked",
    "_policy_for",
    "_run_term_billing_cycle",
]
