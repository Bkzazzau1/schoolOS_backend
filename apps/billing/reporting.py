"""Platform-wide subscription reporting for SkulGen operators (Phase D, Stage 3, Task 8).

Every number here is read directly from BillingInvoice, SchoolEnrollmentCensus and
EnterpriseAgreement - the same authoritative records apps.console's other pages already
show, never a parallel or approximated figure. This module never imports
apps.receivables/apps.smartcollect (a school's own fee collection from parents) and never
will: SkulGen's own subscription revenue and a school's collected fees are deliberately
unrelated ledgers. An OPEN invoice is never counted as revenue - only a PAID one's actual
amount_paid_minor is.
"""

from datetime import datetime, time, timedelta

from django.db.models import Q, Sum
from django.utils import timezone

from apps.schools.models import School

from .models import (
    BillingInvoice,
    CensusStatus,
    EnterpriseAgreement,
    InvoiceStatus,
    SchoolEnrollmentCensus,
    SubscriptionEvent,
    SubscriptionStatus,
)
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


def _scale_to_peak(rows: list[dict], key: str, out_key: str) -> None:
    """Each row's share of the largest value in the list, as a 0-100 percent a
    template can use directly for a bar's height or width."""

    peak = max((row[key] for row in rows), default=0) or 1
    for row in rows:
        row[out_key] = round(row[key] / peak * 100)


def _week_buckets(*, weeks: int, now) -> list[tuple]:
    """weeks calendar-week (Mon-Sun) [start_date, start_at, end_at) windows, oldest first."""

    today = timezone.localdate(now)
    this_week_start = today - timedelta(days=today.weekday())
    windows = []
    for weeks_ago in range(weeks - 1, -1, -1):
        start = this_week_start - timedelta(weeks=weeks_ago)
        end = start + timedelta(days=7)
        start_at = timezone.make_aware(datetime.combine(start, time.min))
        end_at = timezone.make_aware(datetime.combine(end, time.min))
        windows.append((start, start_at, end_at))
    return windows


def weekly_confirmed_revenue(*, weeks: int = 12, now=None) -> dict:
    """Actual cash collected (paid invoices only - an open invoice is never revenue)
    per calendar week, grouped by currency so amounts in different currencies are never
    summed together or compared on one bar scale."""

    now = now or timezone.now()
    windows = _week_buckets(weeks=weeks, now=now)
    currencies = sorted(
        BillingInvoice.objects.filter(status=InvoiceStatus.PAID).values_list("currency", flat=True).distinct()
    )
    report = {}
    for currency in currencies:
        buckets = []
        for week_start, start_at, end_at in windows:
            amount = (
                BillingInvoice.objects.filter(
                    status=InvoiceStatus.PAID,
                    currency=currency,
                    paid_at__gte=start_at,
                    paid_at__lt=end_at,
                ).aggregate(total=Sum("amount_paid_minor"))["total"]
                or 0
            )
            buckets.append({"week_start": week_start, "amount_minor": amount})
        _scale_to_peak(buckets, "amount_minor", "height_pct")
        report[currency] = buckets
    return report


def revenue_by_plan() -> dict:
    """Confirmed revenue (paid invoices only) split by which plan actually earned it,
    grouped by currency - how many schools are on a plan isn't the same question as
    which plan is actually making money."""

    rows = (
        BillingInvoice.objects.filter(status=InvoiceStatus.PAID)
        .values("subscription__plan__name", "currency")
        .annotate(amount_minor=Sum("amount_paid_minor"))
    )
    report: dict[str, list[dict]] = {}
    for row in rows:
        report.setdefault(row["currency"], []).append(
            {
                "plan_name": row["subscription__plan__name"] or "No plan",
                "amount_minor": row["amount_minor"],
            }
        )
    for plan_rows in report.values():
        plan_rows.sort(key=lambda r: r["amount_minor"], reverse=True)
        _scale_to_peak(plan_rows, "amount_minor", "width_pct")
    return dict(sorted(report.items()))


def weekly_cancellations(*, weeks: int = 12, now=None) -> list[dict]:
    """Subscriptions that actually transitioned to cancelled per calendar week, read
    from the append-only SubscriptionEvent history - a churn signal distinct from
    today's live cancelled count, which only shows who's cancelled now, not when."""

    now = now or timezone.now()
    buckets = []
    for week_start, start_at, end_at in _week_buckets(weeks=weeks, now=now):
        count = SubscriptionEvent.objects.filter(
            to_status=SubscriptionStatus.CANCELLED, at__gte=start_at, at__lt=end_at
        ).count()
        buckets.append({"week_start": week_start, "count": count})
    _scale_to_peak(buckets, "count", "height_pct")
    return buckets
