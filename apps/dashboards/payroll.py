from apps.payroll.constants import APPROVED, BATCH, INSTRUCTED, PREPARED, REJECTED

from .data import payloads


def summary(school) -> dict:
    """Every month's batch, newest first, and what is waiting on someone."""
    batches = sorted(payloads(school, BATCH), key=lambda b: b["period"], reverse=True)
    rows = [
        {"period": b["period"], "status": b["status"], "total": b["total"], "staff": len(b["lines"])}
        for b in batches
    ]
    status = lambda s: [r for r in rows if r["status"] == s]  # noqa: E731
    return {
        "batches": rows,
        "latest": rows[0] if rows else None,
        "waitingForApproval": status(PREPARED),
        "waitingForPayment": status(APPROVED),
        "rejected": status(REJECTED),
        "paymentInstructed": status(INSTRUCTED),
        "instructedTotal": sum(r["total"] for r in status(INSTRUCTED)),
    }
