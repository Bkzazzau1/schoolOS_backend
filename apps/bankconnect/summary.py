"""What the school has actually collected, worked out from the payments SchoolOS has received.

Only real money is counted. Test (sandbox) payments are left out unless asked for, and how many were
left out is said, so a demo can never look like the school's income. Money that was reversed or
refunded, and a payment held as a possible duplicate until a person confirms it, is not counted as
collected. Amounts are whole kobo, and only naira is totalled (anything else is counted separately).

How much is still owed comes from the school's fee ledger (apps/receivables), by session and term, and only
once the school has raised charges; until then it says so instead of showing zero.
"""

from datetime import timedelta

from django.db.models import Count, Sum
from django.utils import timezone

from apps.academics.models import AcademicLifecycleStatus, AcademicTerm

from .constants import DEFAULT_CURRENCY, NEEDS_A_PERSON, COLLECTION_PROVIDER_CODES, ConnectionStatus, Direction, ReconStatus
from .models import BankTransaction, CollectionProviderConnection, TransactionAllocation
from .serializers import serialize_transaction_brief

#: Not counted as collected: the money went back, or a person has yet to say it is a real second payment.
NOT_COLLECTED = (ReconStatus.REVERSED, ReconStatus.REFUNDED, ReconStatus.DUPLICATE)
PERIODS = ("today", "week", "term", "all")


def _total(rows) -> dict:
    found = rows.aggregate(amount=Sum("amount_minor"), count=Count("id"))
    return {"amountMinor": found["amount"] or 0, "count": found["count"]}


def _active_term(school):
    return (
        AcademicTerm.objects.filter(session__school=school, status=AcademicLifecycleStatus.ACTIVE)
        .select_related("session").first()
    )


def _period_rows(rows, period: str, today, week_start, term):
    """The rows in a period, judged by the school's own calendar day (Africa/Lagos)."""
    if period == "today":
        return rows.filter(transaction_date__date=today)
    if period == "week":
        return rows.filter(transaction_date__date__gte=week_start, transaction_date__date__lte=today)
    if period == "term" and term is not None:
        return rows.filter(transaction_date__date__gte=term.starts_on, transaction_date__date__lte=term.ends_on)
    return rows


def _reconciliation(counted, credits) -> dict:
    whole = counted.filter(reconciliation_status__in=(ReconStatus.MATCHED, ReconStatus.UNRELATED_INCOME))
    partial = TransactionAllocation.objects.filter(
        transaction__in=counted.filter(reconciliation_status=ReconStatus.PARTIALLY_MATCHED), superseded=False
    )
    reconciled = (whole.aggregate(a=Sum("amount_minor"))["a"] or 0) + (partial.aggregate(a=Sum("amount_minor"))["a"] or 0)
    everything = counted.aggregate(a=Sum("amount_minor"))["a"] or 0
    return {
        "reconciledMinor": reconciled,
        "unreconciledMinor": everything - reconciled,
        "pendingReviewCount": credits.filter(reconciliation_status__in=NEEDS_A_PERSON).count(),
    }


def build(school, *, period: str = "term", include_sandbox: bool = False, recent: int = 10, now=None) -> dict:
    now = timezone.localtime(now or timezone.now())
    today = now.date()
    week_start = today - timedelta(days=today.weekday())
    term = _active_term(school)
    if period not in PERIODS:
        period = "term"
    if period == "term" and term is None:
        period = "all"  # no term is open, so "this term" is not a thing: say so by showing everything

    every = BankTransaction.objects.filter(school=school, direction=Direction.CREDIT)
    credits = every if include_sandbox else every.filter(is_sandbox=False)
    naira = credits.filter(currency=DEFAULT_CURRENCY)
    counted = naira.exclude(reconciliation_status__in=NOT_COLLECTED)
    window = _period_rows(counted, period, today, week_start, term)

    # Only the providers Smart Money Collection offers are counted: a row left by an earlier bank-account model, or by a provider
    # that is no longer offered (Remita), is history and is neither "connected" nor "needing attention".
    connections = CollectionProviderConnection.objects.filter(school=school, provider__in=COLLECTION_PROVIDER_CODES)
    if not include_sandbox:
        connections = connections.filter(is_sandbox=False)
    live = connections.filter(status=ConnectionStatus.CONNECTED)
    attention = connections.filter(status__in=(ConnectionStatus.NEEDS_REAUTH, ConnectionStatus.ERROR))
    active = live.filter(is_active_provider=True).first()

    by_provider = (
        window.order_by().values("connection_id", "provider", "connection__merchant_name", "connection__label", "connection__environment")
        .annotate(amount=Sum("amount_minor"), count=Count("id")).order_by("-amount")
    )

    from apps.receivables import reports  # imported here: receivables reads bank payments too

    owed = reports.school_position(school, today=today)
    return {
        "generatedAt": now.isoformat(),
        "currency": DEFAULT_CURRENCY,
        "period": {
            "key": period,
            "label": {"today": "Today", "week": "This week", "term": term.name if term else "", "all": "All time"}[period],
            "from": {"today": today, "week": week_start, "term": term.starts_on if term else None}.get(period),
            "to": term.ends_on if period == "term" and term else (today if period != "all" else None),
        },
        "available": live.exists(),
        "providers": {
            "connected": live.count(),
            "needAttention": attention.count(),
            "active": (
                {"connectionId": str(active.id), "provider": active.provider, "environment": active.environment, "merchantName": active.merchant_name}
                if active else None
            ),
        },
        "today": _total(_period_rows(counted, "today", today, week_start, term)),
        "thisWeek": _total(_period_rows(counted, "week", today, week_start, term)),
        "thisTerm": _total(_period_rows(counted, "term", today, week_start, term)) if term else None,
        "selected": _total(window),
        "byProvider": [
            {
                "connectionId": str(r["connection_id"]), "provider": r["provider"], "merchantName": r["connection__merchant_name"],
                "label": r["connection__label"], "environment": r["connection__environment"], "amountMinor": r["amount"], "count": r["count"],
            }
            for r in by_provider
        ],
        "reconciliation": _reconciliation(counted, credits),
        "recent": [serialize_transaction_brief(t) for t in credits.order_by("-transaction_date", "-created_at")[:recent]],
        "sandboxIncluded": include_sandbox,
        "sandboxHidden": 0 if include_sandbox else every.filter(is_sandbox=True).count(),
        "otherCurrencyTransactions": credits.exclude(currency=DEFAULT_CURRENCY).count(),
        # What the school is still owed, by session and term, from its fee ledger. Not available until charges exist.
        "outstandingFeesAvailable": owed["available"],
        "receivables": owed,
    }
