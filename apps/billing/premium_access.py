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

from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from .academic_periods import active_session_for_school, active_term_for_school
from .models import BillingInvoice, InvoiceStatus, SchoolBillingMeterSnapshot
from .pricing import PricingPolicyUndefined, fixed_fee_for_school
from .services import ensure_organization_subscription, resolved_entitlements

#: Verified against real, currently-implemented code (apps/cbt/services.py's automatic scoring
#: in submit_attempt(), reached only from CbtTestHandler's "publish" action, and
#: apps/report_cards/services.py's generate_report_card()/compile_class_report_cards(), reached
#: only from ReportCardBatchHandler's "compile" action) - not carried over from any earlier,
#: unverified proposal.
CBT_EXAM_PUBLISH = "cbt_exam_publish"
REPORT_CARD_GENERATION = "report_card_generation"
PREMIUM_CODES = (CBT_EXAM_PUBLISH, REPORT_CARD_GENERATION)


def current_school_invoice(school, *, as_of=None) -> BillingInvoice | None:
    """The invoice that covers this specific school's current billing period, if any - the one
    source of truth both school_premium_access() and the school-scoped entitlement API read,
    so a display can never disagree with the actual gate.

    Prefers a per-school invoice (BillingInvoice.school, set on every invoice issued since the
    per-school independent billing change); a sibling school's different period or a different
    organization's school is never matched. Falls back to a historical, pre-Phase-C combined
    invoice (``school`` is null on those) only when no per-school invoice exists for this
    period at all, and only one that actually covers this school (one of its
    BillingInvoiceLine rows names it) - paid or not; callers that care only about payment use
    school_premium_access() instead.
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
    if invoice is not None or school.organization_id is None:
        return invoice
    return (
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


def school_premium_access(school, *, as_of=None) -> bool:
    """Whether this specific school's current billing period has a paid invoice covering it.

    Fails closed: no invoice yet for the period covering `as_of` means premium stays locked,
    never silently unlocked just because the organization's lifecycle status looks healthy.
    A historical invoice (see current_school_invoice()) only ever unlocks access when it was
    itself PAID - one that was never paid gives no fallback access, this never unlocks a
    payment that was never confirmed, it only preserves one that genuinely already was.
    """

    invoice = current_school_invoice(school, as_of=as_of)
    return invoice is not None and invoice.status == InvoiceStatus.PAID


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


def resolve_school_entitlements(school) -> dict[str, dict]:
    """A read-only, non-raising view of require_premium_entitlement()'s own two real checks -
    the source of truth the school-scoped entitlement API (apps.billing.views) reads for
    display. Distinguishes the three things Phase D's brief requires kept separate, as three
    separate fields rather than one collapsed boolean:

    - ``planEnabled``: the organization's commercial plan entitlement switch
      (resolved_entitlements()) - independent of any one school's payment.
    - ``schoolPaid``: this specific school's own current-period payment
      (school_premium_access()) - independent of the plan switch and of every sibling school.
    - ``available``: what require_premium_entitlement() would actually decide today, called
      directly rather than re-derived, so this can never drift from the real gate's own
      message. (The top-level ``premiumEnforcementEnabled`` flag is the fourth, orthogonal
      dimension: whether any of this is actually enforced yet at all.)

    A school with no organization is outside SkulGen's SaaS billing entirely (the same
    legacy exemption require_premium_entitlement() itself has) - planEnabled and schoolPaid
    both read as true for it, matching available.
    """

    organization = school.organization
    resolved = resolved_entitlements(ensure_organization_subscription(organization)) if organization else {}

    result = {}
    for code in PREMIUM_CODES:
        entry = resolved.get(code)
        plan_enabled = organization is None or bool(entry and entry["enabled"])
        school_paid = organization is None or school_premium_access(school)
        try:
            require_premium_entitlement(school, code=code)
        except PermissionDenied as refused:
            result[code] = {
                "planEnabled": plan_enabled,
                "schoolPaid": school_paid,
                "available": False,
                "reason": str(refused),
            }
        else:
            result[code] = {
                "planEnabled": plan_enabled,
                "schoolPaid": school_paid,
                "available": True,
                "reason": "",
            }
    return result


def _serialize_term_brief(term) -> dict | None:
    if term is None:
        return None
    return {
        "id": str(term.id),
        "code": term.code,
        "name": term.name,
        "startsOn": term.starts_on.isoformat(),
        "endsOn": term.ends_on.isoformat(),
    }


def _serialize_session_brief(session) -> dict | None:
    if session is None:
        return None
    return {
        "id": str(session.id),
        "code": session.code,
        "name": session.name,
        "startsOn": session.starts_on.isoformat(),
        "endsOn": session.ends_on.isoformat(),
    }


def serialize_school_entitlement_status(school, *, viewer_role: str | None, billing_authority: bool) -> dict:
    """The full school-scoped subscription/entitlement payload - built entirely from this
    school's own state (its own academic calendar, its own census, its own invoice; never the
    organization's overall accessMode and never a sibling school's payment). Sensitive
    commercial detail (invoice id/number, due date, exact amounts) is included only when
    `billing_authority` is true - every other field here is safe for any active member of the
    school to see, including why a premium capability is currently unavailable.
    """

    now = timezone.now()
    today = timezone.localdate()
    organization = school.organization

    term = active_term_for_school(school, as_of_date=today)
    session = active_session_for_school(school, as_of_date=today)
    invoice = current_school_invoice(school, as_of=now) if organization is not None else None

    if organization is None:
        payment_status = "not_applicable"
    elif invoice is None:
        payment_status = "no_invoice_yet"
    elif invoice.status == InvoiceStatus.PAID:
        payment_status = "paid"
    else:
        payment_status = "unpaid"

    meter = (
        SchoolBillingMeterSnapshot.objects.filter(school=school, authoritative=True)
        .order_by("-measured_at", "-id")
        .first()
    )
    enrollment = None
    if meter is not None:
        enrollment = {
            "billableStudentCount": meter.billable_student_count,
            "measuredAt": meter.measured_at.isoformat(),
            "bracketMinStudents": None,
            "bracketMaxStudents": None,
            "pricingSource": None,
            "undefinedReason": None,
        }
        try:
            fee = fixed_fee_for_school(
                school, billable_student_count=meter.billable_student_count, as_of=now
            )
        except PricingPolicyUndefined as problem:
            # Never invents a price: zero enrollment, or above every bracket with no
            # enterprise agreement on file, surfaces as an explicit reason, not a number.
            enrollment["undefinedReason"] = str(problem)
        else:
            enrollment["bracketMinStudents"] = fee.min_students
            enrollment["bracketMaxStudents"] = fee.max_students
            enrollment["pricingSource"] = fee.source

    billing = None
    if billing_authority and invoice is not None:
        billing = {
            "organizationId": str(organization.id),
            "invoiceId": str(invoice.id),
            "invoiceNumber": invoice.number,
            "status": invoice.status,
            "amountDueMinor": invoice.amount_due_minor,
            "amountPaidMinor": invoice.amount_paid_minor,
            "currency": invoice.currency,
            "dueAt": invoice.due_at.isoformat() if invoice.due_at else None,
            "issuedAt": invoice.issued_at.isoformat(),
            "payable": invoice.status == InvoiceStatus.OPEN,
        }

    return {
        "schoolId": str(school.id),
        "schoolName": school.name,
        "organizationId": str(organization.id) if organization else None,
        "viewerRole": viewer_role,
        "canManageBilling": billing_authority,
        "premiumEnforcementEnabled": settings.PREMIUM_ENFORCEMENT_ENABLED,
        "academicTerm": _serialize_term_brief(term),
        "academicSession": _serialize_session_brief(session),
        "paymentStatus": payment_status,
        "currentPeriod": (
            {"start": invoice.period_start.isoformat(), "end": invoice.period_end.isoformat()}
            if invoice is not None and invoice.period_start and invoice.period_end
            else None
        ),
        "enrollment": enrollment,
        "entitlements": resolve_school_entitlements(school),
        "billing": billing,
    }
