"""Per-school, per-term premium-feature access - the second half of premium entitlement
resolution, alongside the organization-level commercial-plan check (whether a feature code is
enabled at all, independent of any one school's payment).

A premium feature requires BOTH: the organization's commercial plan to have that entitlement
enabled (a plan-wide, commercial on/off switch - independent of lifecycle status), AND this
specific school's own current billing period to have a paid invoice
(school_premium_access()). Deliberately does NOT also require the organization's lifecycle
access_mode to be "full": since Phase C's independent per-school billing change, each active
school gets its own invoice, paid independently of its siblings, so the organization's
overall status can be dragged toward "restricted" by one unpaid school without that school's
paid sibling ever losing its own premium access - "do not rely solely on the organization-wide
subscription status" is explicit in the brief this implements. A school whose organization
drops to "restricted"/"suspended" still loses *essential* access the ordinary way
(apps.core.permissions); premium access here is judged solely by this school's own invoices.

Nothing in this module is wired into any live feature's request path yet. These functions are
the reusable foundation the freemium policy calls for; which premium entity types or actions
actually call require_premium_entitlement(), and when that goes live, are separate, explicitly
authorized steps - see the design report for the full candidate list and why each one either
qualifies today or doesn't (performance analytics, AI tooling and "advanced financial
analytics" all turned out to have no backend implementation to gate yet, verified against the
actual repository rather than assumed from an earlier proposal).
"""

from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from .models import BillingInvoice, InvoiceStatus
from .services import ensure_organization_subscription, resolved_entitlements

#: Verified against real, currently-implemented code (apps/cbt/services.py's automatic scoring
#: in submit_attempt(), reached only from CbtTestHandler's "publish" action, and
#: apps/report_cards/services.py's generate_report_card()/compile_class_report_cards(), reached
#: only from ReportCardBatchHandler's "compile" action) - not carried over from any earlier,
#: unverified proposal.
CBT_EXAM_PUBLISH = "cbt_exam_publish"
REPORT_CARD_GENERATION = "report_card_generation"


def school_premium_access(school, *, as_of=None) -> bool:
    """Whether this specific school's current billing period has a paid invoice covering it.

    Fails closed: no invoice yet for the period covering `as_of` means premium stays locked,
    never silently unlocked just because the organization's lifecycle status looks healthy.
    Queries BillingInvoice.school directly (set on every invoice issued since the per-school
    independent billing change), so one school's paid period can never unlock a sibling
    school's different period or a different organization's school - each school's own
    invoice carries its own status, settled independently through the ordinary Paystack
    checkout/webhook flow, with the usual idempotency guarantees unchanged.

    Falls back to a historical, pre-Phase-C combined invoice (``school`` is null on those -
    see BillingInvoice's own docstring) only when no per-school invoice exists for this period
    at all, and only when that historical invoice both actually covers this school (one of its
    BillingInvoiceLine rows names it) and is itself PAID. A historical invoice that was never
    paid gives no fallback access - this never unlocks a payment that was never confirmed, it
    only preserves one that genuinely already was.
    """

    as_of = as_of or timezone.now()
    invoice = (
        BillingInvoice.objects.filter(
            school=school,
            period_start__lte=as_of,
            period_end__gt=as_of,
        )
        .order_by("-issued_at", "-id")
        .first()
    )
    if invoice is not None:
        return invoice.status == InvoiceStatus.PAID
    if school.organization_id is None:
        return False
    historical = (
        BillingInvoice.objects.filter(
            school__isnull=True,
            organization_id=school.organization_id,
            period_start__lte=as_of,
            period_end__gt=as_of,
            lines__school=school,
        )
        .order_by("-issued_at", "-id")
        .first()
    )
    return historical is not None and historical.status == InvoiceStatus.PAID


def require_premium_entitlement(school, *, code: str, current_value: int | None = None) -> None:
    """Raises unless both premium conditions hold for this school: its organization's
    commercial plan has `code` enabled at all (a plan-wide switch, not an access_mode check),
    and this school's own current billing period is actually paid (school_premium_access()).

    A school with no organization is outside SkulGen's SaaS billing entirely - the same,
    narrowly-scoped legacy exemption apps.core.permissions already gives it for the ordinary
    subscription gate (schools created before the SaaS account layer). Every school created
    through the normal provisioning flow (apps.organizations.services.provision_school) is
    always attached to an organization, so this exemption has no path to apply to a new
    school going forward - it is not an unintended permanent free-premium bypass, only a
    reviewed accommodation for pre-existing data.
    """

    organization = school.organization
    if organization is None:
        return
    subscription = ensure_organization_subscription(organization)
    resolved = resolved_entitlements(subscription).get(code)
    if resolved is None or not resolved["enabled"]:
        raise PermissionDenied(
            "Your current SkulGen plan does not include this feature."
        )
    if (
        current_value is not None
        and resolved["limit"] is not None
        and current_value >= resolved["limit"]
    ):
        raise PermissionDenied(
            "You have reached the limit for this feature."
        )
    if not school_premium_access(school):
        raise PermissionDenied(
            "This feature needs this school's current term subscription to be paid."
        )
