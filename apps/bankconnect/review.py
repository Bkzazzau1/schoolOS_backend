"""A person's decisions on a payment: what the engine cannot settle, they settle here.

Every decision is recorded as a `ReconciliationDecision` (who, what, why, and the payment before and
after) and nothing is ever deleted: a new decision supersedes the allocations of the last one and
`reopen` puts a payment back in the queue. Two reviewers acting at once are safe - the payment is
locked, and a client may send the status it was looking at (`expectedStatus`) so that a decision
made on stale information is refused instead of silently overwriting a colleague's.
"""

from uuid import UUID

from django.db import transaction
from rest_framework.exceptions import NotFound

from apps.students.models import Student

from apps.receivables import payments as receivable_payments
from apps.receivables.errors import Refused as ReceivablesRefused

from .provider_connections import BankRejected
from .constants import Direction, Purpose, ReconStatus
from .models import BankTransaction, ReconciliationDecision, TransactionAllocation

ACTIONS = ("assign", "split", "unrelated_income", "duplicate", "investigate", "reversed", "refunded", "reopen")
#: A person must say why they did these: they change what counts as the school's money.
NOTE_REQUIRED = ("unrelated_income", "duplicate", "investigate", "reversed", "refunded", "reopen")
DECIDED = (
    ReconStatus.MATCHED, ReconStatus.PARTIALLY_MATCHED, ReconStatus.UNRELATED_INCOME, ReconStatus.DUPLICATE,
    ReconStatus.REVERSED, ReconStatus.REFUNDED, ReconStatus.INVESTIGATING,
)
#: Money that went back out. Only `reopen` can change these.
FINAL = (ReconStatus.REVERSED, ReconStatus.REFUNDED)
MAX_SPLITS = 20
MAX_NOTE = 500

_STATUS_FOR_ACTION = {
    "unrelated_income": ReconStatus.UNRELATED_INCOME,
    "duplicate": ReconStatus.DUPLICATE,
    "investigate": ReconStatus.INVESTIGATING,
    "reversed": ReconStatus.REVERSED,
    "refunded": ReconStatus.REFUNDED,
    "reopen": ReconStatus.REQUIRES_REVIEW,
}


def snapshot(row: BankTransaction) -> dict:
    """The parts of a payment a decision can change, for the before/after record."""
    return {
        "status": str(row.reconciliation_status),
        "confidence": row.reconciliation_confidence,
        "duplicateOf": str(row.duplicate_of_id) if row.duplicate_of_id else None,
        "allocations": [
            {"studentId": str(a.student_id), "purpose": a.purpose, "amountMinor": a.amount_minor}
            for a in row.allocations.filter(superseded=False)
        ],
    }


def _uuid(value, what: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError:
        raise BankRejected(f"That {what} is not valid.", "invalid_reference")


def _student(school, value) -> Student:
    student = Student.objects.filter(school=school, id=_uuid(value, "student")).first()
    if student is None:
        raise BankRejected("That student is not at this school.", "unknown_student")
    return student


def _purpose(value, default: str) -> str:
    if value in (None, ""):
        return default
    if value not in Purpose.values:
        raise BankRejected("Choose what this payment was for.", "invalid_purpose")
    return value


def _amount(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BankRejected("Each amount must be a whole number of kobo greater than zero.", "invalid_amount")
    return value


def _plan_allocations(row, action, student_id, purpose, allocations) -> list[dict]:
    default = _purpose(purpose, row.connection.purpose)
    if action == "assign":
        return [{"student": _student(row.school, student_id), "purpose": default, "amountMinor": row.amount_minor}]
    if not isinstance(allocations, list) or not 1 <= len(allocations) <= MAX_SPLITS:
        raise BankRejected(f"Split the payment between 1 and {MAX_SPLITS} students.", "invalid_split")
    plan, seen = [], set()
    for item in allocations:
        if not isinstance(item, dict):
            raise BankRejected("Each part of a split needs a student and an amount.", "invalid_split")
        student = _student(row.school, item.get("studentId"))
        part = {"student": student, "purpose": _purpose(item.get("purpose"), default), "amountMinor": _amount(item.get("amountMinor"))}
        if (student.id, part["purpose"]) in seen:
            raise BankRejected("A student can appear once per purpose in a split.", "invalid_split")
        seen.add((student.id, part["purpose"]))
        plan.append(part)
    if sum(p["amountMinor"] for p in plan) > row.amount_minor:
        raise BankRejected("The parts add up to more than the payment.", "invalid_split")
    return plan


def _original(row, duplicate_of):
    if duplicate_of in (None, ""):
        return row.duplicate_of
    original = BankTransaction.objects.filter(
        school=row.school, direction=Direction.CREDIT, id=_uuid(duplicate_of, "payment")
    ).exclude(id=row.id).first()
    if original is None:
        raise BankRejected("That payment was not found at this school.", "unknown_payment")
    if original.duplicate_of_id == row.id:
        raise BankRejected("That payment is already recorded as a duplicate of this one.", "duplicate_cycle")
    return original


def decide(
    membership, transaction_id, *, action, expected_status=None, student_id=None, purpose=None,
    allocations=None, note="", duplicate_of=None,
) -> BankTransaction:
    if action not in ACTIONS:
        raise BankRejected("That is not something a payment can be marked as.", "unknown_action")
    note = " ".join(str(note or "").split())
    if len(note) > MAX_NOTE:
        raise BankRejected(f"A note can be at most {MAX_NOTE} characters.", "invalid_note")
    if action in NOTE_REQUIRED and not note:
        raise BankRejected("Say why: a note is needed for this.", "note_required")

    with transaction.atomic():
        row = (
            BankTransaction.objects.select_for_update().select_related("connection")
            .filter(school=membership.school, id=transaction_id).first()
        )
        if row is None:
            raise NotFound("That payment was not found.")
        if row.direction != Direction.CREDIT:
            raise BankRejected("Only money received can be reconciled.", "not_a_credit")
        if expected_status and expected_status != row.reconciliation_status:
            raise BankRejected("Someone has changed this payment since you opened it. Refresh and look again.", "stale")
        current = row.reconciliation_status
        if action == "reopen" and current not in DECIDED:
            raise BankRejected("This payment is already waiting for review.", "not_decided")
        if action != "reopen" and current in FINAL:
            raise BankRejected("This payment was reversed or refunded. Reopen it before changing it.", "final")

        before = snapshot(row)
        # Whatever this payment had been put towards in the school's fee ledger comes back out first, so the
        # person's decision starts from the truth. If credit from it has already been used elsewhere, those
        # uses are undone; if that cannot be done, the decision is refused and nothing changes.
        try:
            receivable_payments.release(row, actor=membership, reason=note or action)
        except ReceivablesRefused as refused:
            raise BankRejected(refused.message, refused.code)
        plan = _plan_allocations(row, action, student_id, purpose, allocations) if action in ("assign", "split") else []
        if plan:
            covered = sum(p["amountMinor"] for p in plan)
            new_status = ReconStatus.MATCHED if covered == row.amount_minor else ReconStatus.PARTIALLY_MATCHED
        else:
            new_status = _STATUS_FOR_ACTION[action]
        new_original = _original(row, duplicate_of) if action == "duplicate" else (
            None if action in ("assign", "split", "reopen") else row.duplicate_of
        )

        after = {
            "status": str(new_status),
            "confidence": 100 if plan else row.reconciliation_confidence,
            "duplicateOf": str(new_original.id) if new_original else None,
            "allocations": [
                {"studentId": str(p["student"].id), "purpose": p["purpose"], "amountMinor": p["amountMinor"]} for p in plan
            ],
        }
        decision = ReconciliationDecision.objects.create(
            school=row.school, transaction=row, action=action, actor=membership, note=note, before=before, after=after
        )
        # Allocations only stand while the payment is matched: any other decision retires them.
        row.allocations.filter(superseded=False).update(superseded=True)
        for part in plan:
            TransactionAllocation.objects.create(
                school=row.school, transaction=row, student=part["student"], purpose=part["purpose"],
                amount_minor=part["amountMinor"], source="manual", decision=decision,
            )
        row.reconciliation_status = new_status
        row.reconciliation_confidence = after["confidence"]
        row.duplicate_of = new_original
        row.save()
        if new_status in (ReconStatus.MATCHED, ReconStatus.PARTIALLY_MATCHED):
            # The person chose the student(s): put the money towards their family's charges, their student first.
            try:
                receivable_payments.settle(row, decision=decision, actor=membership)
            except ReceivablesRefused as refused:
                raise BankRejected(refused.message, refused.code)
    return row
