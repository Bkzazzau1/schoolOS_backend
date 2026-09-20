from apps.concessions.constants import APPROVED, CONCESSION, DECLINED, PENDING

from .data import payloads


def summary(school) -> dict:
    requests = payloads(school, CONCESSION)
    of = lambda status: [r for r in requests if r["status"] == status]  # noqa: E731
    approved = of(APPROVED)
    return {
        "pending": {"count": len(of(PENDING)), "amount": sum(r["amount"] for r in of(PENDING))},
        "approved": {
            "count": len(approved),
            "amount": sum(r["amount"] for r in approved),
            "studentsSupported": len({r["student"].strip().lower() for r in approved}),
            "byType": {
                kind: sum(r["amount"] for r in approved if r["type"] == kind) for kind in ("scholarship", "discount")
            },
        },
        "declined": {"count": len(of(DECLINED))},
    }
