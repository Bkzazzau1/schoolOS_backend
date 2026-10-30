"""Per-school, per-term premium-feature access - the second half of premium entitlement
resolution, alongside the existing organization-level apps.billing.services.require_entitlement.

A premium feature requires BOTH: the organization's plan to have that entitlement enabled with
a healthy (full) access mode - the existing require_entitlement() check, unchanged - AND this
specific school's current billing period to have a paid invoice. The second check is what
makes premium access per-school and per-term rather than just per-organization: one
organization's invoice can cover several schools, and paying it unlocks every school that
invoice actually covers for the period it actually covers, never a sibling school on a
different (later or earlier) period that invoice doesn't cover.

Nothing in this module is wired into any live feature's request path yet. These functions are
the reusable foundation the freemium policy calls for; which premium entity types or views
actually call require_premium_entitlement(), and when that goes live, are separate, explicitly
authorized steps - see the design report for the full candidate list and why each one either
qualifies today or doesn't (performance analytics, AI tooling and "advanced financial
analytics" all turned out to have no backend implementation to gate yet, verified against the
actual repository rather than assumed from an earlier proposal).
"""

from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from .models import BillingInvoiceLine, InvoiceStatus
from .services import require_entitlement

#: Verified against real, currently-implemented code (apps/cbt/services.py's automatic scoring
#: in submit_attempt(), and apps/report_cards/services.py's generate_report_card() /
#: compile_class_report_cards()) - not carried over from any earlier, unverified proposal.
CBT_SUBMISSION = "cbt_submission"
REPORT_CARD_GENERATION = "report_card_generation"


def school_premium_access(school, *, as_of=None) -> bool:
    """Whether this specific school's current billing period has a paid invoice covering it.

    Fails closed: no invoice yet for the period covering `as_of` means premium stays locked,
    never silently unlocked just because the organization's lifecycle status looks healthy.
    Scoped to this school's own BillingInvoiceLine, not the organization as a whole, so one
    school's paid period can never unlock a sibling school's different period. Today every
    school in one organization invoice shares that invoice's one period and one paid/unpaid
    status (multi-school term billing with independent per-school periods is a later phase -
    see the design report's item on this), so two schools on the same combined invoice are
    correctly unlocked or locked together; they are never conflated with a different
    organization's schools or a different period's invoice.
    """

    as_of = as_of or timezone.now()
    line = (
        BillingInvoiceLine.objects.filter(
            school=school,
            period_start__lte=as_of,
            period_end__gt=as_of,
        )
        .select_related("invoice")
        .order_by("-invoice__issued_at", "-id")
        .first()
    )
    if line is None:
        return False
    return line.invoice.status == InvoiceStatus.PAID


def require_premium_entitlement(school, *, code: str, current_value: int | None = None) -> None:
    """Raises unless both premium conditions hold for this school: its organization's plan has
    `code` enabled under a full access mode (require_entitlement, unchanged), and this school's
    own current billing period is actually paid (school_premium_access).

    A school with no organization is outside SkulGen's SaaS billing entirely - the same
    exemption apps.core.permissions already gives it for the ordinary subscription gate - so
    it is never blocked here either.
    """

    organization = school.organization
    if organization is None:
        return
    require_entitlement(organization, code, current_value=current_value)
    if not school_premium_access(school):
        raise PermissionDenied(
            "This feature needs this school's current term subscription to be paid."
        )
