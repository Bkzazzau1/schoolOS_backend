"""Platform-wide subscription reporting for SkulGen operators (Phase D, Stage 3, Task 8).

Every number here is read directly from BillingInvoice, SchoolEnrollmentCensus and
EnterpriseAgreement - the same authoritative records apps.console's other pages already
show, never a parallel or approximated figure. This module never imports
apps.receivables/apps.smartcollect (a school's own fee collection from parents) and never
will: SkulGen's own subscription revenue and a school's collected fees are deliberately
unrelated ledgers. An OPEN invoice is never counted as revenue - only a PAID one's actual
amount_paid_minor is.
"""

from datetime import timedelta

from django.db.models import Q, Sum
from django.utils import timezone

from apps.schools.models import School

from .models import BillingInvoice, CensusStatus, EnterpriseAgreement, InvoiceStatus, SchoolEnrollmentCensus
from .premium_access import current_school_invoice


def platform_subscription_report(*, as_of=None, expiring_within_days: int = 30) -> dict:
    as_of = as_of or timezone.now()
    schools = list(
        School.objects.filter(is_active=True, organization__isnull=False, organization__is_active=True)
    )
    expiring_cutoff = as_of + timedelta(days=expiring_within_days)

    paid_count = 0
    awaiting_payment_count = 0
    expiring_soon_count = 0
    for school in schools:
        invoice = current_school_invoice(school, as_of=as_of)
        if invoice is None:
            continue
        if invoice.status == InvoiceStatus.PAID:
            paid_count += 1
            if invoice.period_end is not None and as_of <= invoice.period_end <= expiring_cutoff:
                expiring_soon_count += 1
        elif invoice.status == InvoiceStatus.OPEN:
            awaiting_payment_count += 1

    # Grouped by currency rather than assumed NGN-only, so a future second currency is never
    # silently summed into the wrong total.
    revenue_by_currency = list(
        BillingInvoice.objects.filter(status=InvoiceStatus.PAID)
        .values("currency")
        .annotate(amount_minor=Sum("amount_paid_minor"))
        .order_by("currency")
    )

    enterprise_school_count = (
        EnterpriseAgreement.objects.filter(effective_from__lte=as_of)
        .filter(Q(effective_until__isnull=True) | Q(effective_until__gt=as_of))
        .values("school_id")
        .distinct()
        .count()
    )

    census_review_school_count = (
        SchoolEnrollmentCensus.objects.filter(status=CensusStatus.NEEDS_REVIEW)
        .values("school_id")
        .distinct()
        .count()
    )

    return {
        "asOf": as_of.isoformat(),
        "totalSubscribedSchools": len(schools),
        "schoolsWithActivePaidSubscriptions": paid_count,
        "schoolsAwaitingPayment": awaiting_payment_count,
        "schoolsExpiringSoon": expiring_soon_count,
        "expiringWithinDays": expiring_within_days,
        "unpaidInvoiceCount": BillingInvoice.objects.filter(status=InvoiceStatus.OPEN).count(),
        "confirmedRevenueByCurrency": [
            {"currency": row["currency"], "amountMinor": row["amount_minor"]} for row in revenue_by_currency
        ],
        "schoolsOnEnterpriseAgreements": enterprise_school_count,
        "schoolsAwaitingCensusReview": census_review_school_count,
    }
