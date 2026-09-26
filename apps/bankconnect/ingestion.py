"""Taking a provider's transaction into SchoolOS - exactly once, however many times it is delivered.

Every way in (a webhook, a scheduled poll, "Sync now") comes through `ingest`. It is idempotent by
construction: the database refuses a second row for the same external id on a connection, and for
the same provider session at a school, so a redelivered webhook, an overlapping sync or two workers
racing can never make a second record. The loser of a race is told "already have it" and gets the
first row back.
"""

from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import identifiers
from .constants import DEFAULT_CURRENCY, Direction, ReconStatus
from .models import BankTransaction

CREATED, DUPLICATE, INVALID = "created", "duplicate", "invalid"


@dataclass(frozen=True)
class IngestResult:
    outcome: str
    transaction: BankTransaction | None = None

    @property
    def created(self) -> bool:
        return self.outcome == CREATED


def _text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _currency(value) -> str | None:
    """An ISO-style three-letter code, or None: a wrong code must be refused, never trimmed to fit."""
    code = str(value or DEFAULT_CURRENCY).strip().upper()
    return code if len(code) == 3 and code.isalpha() else None


def _valid(tx) -> bool:
    return (
        bool(_text(tx.external_transaction_id, 160))
        and tx.direction in Direction.values
        and isinstance(tx.amount_minor, int)
        and not isinstance(tx.amount_minor, bool)
        and tx.amount_minor > 0
        and isinstance(tx.transaction_date, datetime)
        and _currency(tx.currency) is not None
    )


def _existing(connection, external_id: str, session_id: str):
    found = BankTransaction.objects.filter(connection=connection, external_transaction_id=external_id).first()
    if found is None and session_id:
        found = BankTransaction.objects.filter(
            school=connection.school, provider=connection.provider, provider_session_id=session_id
        ).first()
    return found


def ingest(connection, tx) -> IngestResult:
    """Record one normalised transaction. Returns whether it was new; never raises for a repeat."""
    if not _valid(tx):
        return IngestResult(INVALID)
    external_id = _text(tx.external_transaction_id, 160)
    session_id = _text(tx.provider_session_id, 160)
    when = tx.transaction_date
    if timezone.is_naive(when):
        when = when.replace(tzinfo=dt_timezone.utc)
    fields = dict(
        school=connection.school,
        connection=connection,
        provider=connection.provider,
        bank_name=connection.bank_name,
        bank_account_name=connection.account_name,
        masked_account_number=connection.account_mask,
        external_transaction_id=external_id,
        transaction_reference=_text(tx.transaction_reference, 160),
        provider_session_id=session_id,
        transaction_type=_text(tx.transaction_type, 40),
        direction=tx.direction,
        amount_minor=tx.amount_minor,
        currency=_currency(tx.currency),
        sender_name=_text(tx.sender_name, 200),
        # The sender's full number is masked here and never stored: nothing needs it.
        sender_account_mask=identifiers.mask_account(tx.sender_account_number) if tx.sender_account_number else "",
        sender_bank=_text(tx.sender_bank, 120),
        narration=_text(tx.narration, 500),
        transaction_date=when,
        value_date=tx.value_date,
        balance_after_minor=tx.balance_after_minor,
        raw_provider_reference=_text(tx.raw_provider_reference, 200),
        is_sandbox=connection.is_sandbox,
        # Only money coming in is matched to students; money going out is kept, not reconciled.
        reconciliation_status=ReconStatus.UNMATCHED if tx.direction == Direction.CREDIT else ReconStatus.NOT_APPLICABLE,
    )
    try:
        with transaction.atomic():  # a savepoint: losing a race must not poison the caller's transaction
            row = BankTransaction.objects.create(**fields)
    except IntegrityError:
        row = _existing(connection, external_id, session_id)
        if row is None:
            raise  # not a repeat: something else is wrong, and hiding it would lose money silently
        return IngestResult(DUPLICATE, row)
    return IngestResult(CREATED, row)
