import uuid

from django.db import models
from django.db.models import Q

from apps.schools.models import Membership, School
from apps.students.models import Student

from .constants import (
    DEFAULT_CURRENCY,
    ConnectionStatus,
    ConnectionType,
    Direction,
    Purpose,
    ReconStatus,
)


class BankConnection(models.Model):
    """One of the school's own bank or collection-provider accounts, connected to SchoolOS.

    Everything on this row is safe to show a signed-in finance user: bank, verified account
    name, the masked number and its status. The credential itself exists only as
    `sealed_credentials` (see vault.py) and is never serialised anywhere.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="bank_connections")
    provider = models.CharField(max_length=40)
    connection_type = models.CharField(max_length=24, choices=ConnectionType.choices)

    #: What the bank itself reports about the account, once verified.
    bank_name = models.CharField(max_length=120, blank=True)
    account_name = models.CharField(max_length=200, blank=True)
    account_mask = models.CharField(max_length=16, blank=True)
    #: A keyed, one-way fingerprint of the account number: the same account cannot be connected
    #: twice to one school, and the number itself is never stored.
    account_fingerprint = models.CharField(max_length=64, blank=True)

    purpose = models.CharField(max_length=16, choices=Purpose.choices, default=Purpose.GENERAL)
    label = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=16, choices=ConnectionStatus.choices, default=ConnectionStatus.PENDING)

    sealed_credentials = models.BinaryField(blank=True, default=b"")
    token_expires_at = models.DateTimeField(null=True, blank=True)
    #: Only ever a hash: the token itself is shown once, when it is issued.
    webhook_token_hash = models.CharField(max_length=64, blank=True, db_index=True)

    sync_cursor = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    #: A short code, never a provider's own message: those can echo what was sent.
    last_error_code = models.CharField(max_length=40, blank=True)
    provider_meta = models.JSONField(default=dict, blank=True)
    is_sandbox = models.BooleanField(default=False)

    created_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    disconnected_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "provider", "account_fingerprint"],
                condition=~Q(account_fingerprint="") & ~Q(status=ConnectionStatus.REVOKED),
                name="unique_live_bank_account_per_school",
            ),
        ]
        indexes = [models.Index(fields=["school", "status"])]

    def __str__(self):
        return f"{self.provider} {self.account_mask} ({self.school_id})"


class BankTransaction(models.Model):
    """One movement on a connected account, in SchoolOS's own canonical shape - whichever bank
    or provider it came from."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="bank_transactions")
    #: PROTECT: a connection with history is disconnected, never deleted.
    connection = models.ForeignKey(BankConnection, on_delete=models.PROTECT, related_name="transactions")

    #: A snapshot of where it landed, so it still reads correctly if the connection is renamed.
    provider = models.CharField(max_length=40)
    bank_name = models.CharField(max_length=120, blank=True)
    bank_account_name = models.CharField(max_length=200, blank=True)
    masked_account_number = models.CharField(max_length=16, blank=True)

    external_transaction_id = models.CharField(max_length=160)
    transaction_reference = models.CharField(max_length=160, blank=True)
    provider_session_id = models.CharField(max_length=160, blank=True)
    transaction_type = models.CharField(max_length=40, blank=True)
    direction = models.CharField(max_length=8, choices=Direction.choices)
    #: Whole minor units (kobo), always positive; direction says which way it went.
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)

    sender_name = models.CharField(max_length=200, blank=True)
    #: Masked only. The sender's full account number is never stored: nothing here needs it.
    sender_account_mask = models.CharField(max_length=16, blank=True)
    sender_bank = models.CharField(max_length=120, blank=True)
    narration = models.CharField(max_length=500, blank=True)
    transaction_date = models.DateTimeField()
    value_date = models.DateField(null=True, blank=True)
    balance_after_minor = models.BigIntegerField(null=True, blank=True)
    raw_provider_reference = models.CharField(max_length=200, blank=True)
    is_sandbox = models.BooleanField(default=False)

    reconciliation_status = models.CharField(
        max_length=20, choices=ReconStatus.choices, default=ReconStatus.UNMATCHED
    )
    reconciliation_confidence = models.PositiveSmallIntegerField(default=0)
    #: Why the engine decided what it did: every signal and its weight, in plain words.
    match_reasons = models.JSONField(default=list, blank=True)
    engine_version = models.CharField(max_length=16, blank=True)
    duplicate_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="duplicates")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-transaction_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "external_transaction_id"], name="unique_transaction_per_connection"
            ),
            models.UniqueConstraint(
                fields=["school", "provider", "provider_session_id"],
                condition=~Q(provider_session_id=""),
                name="unique_provider_session_per_school",
            ),
            models.CheckConstraint(condition=Q(amount_minor__gt=0), name="bank_transaction_amount_positive"),
        ]
        indexes = [
            models.Index(fields=["school", "-transaction_date"]),
            models.Index(fields=["school", "reconciliation_status"]),
        ]


class ReconciliationDecision(models.Model):
    """Append-only: everything the engine or a person decided about a transaction."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    transaction = models.ForeignKey(BankTransaction, on_delete=models.CASCADE, related_name="decisions")
    action = models.CharField(max_length=24)
    #: Empty when the engine acted on its own.
    actor = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=500, blank=True)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["at", "id"]


class TransactionAllocation(models.Model):
    """How much of a transaction has been assigned to which student, for which purpose."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    transaction = models.ForeignKey(BankTransaction, on_delete=models.CASCADE, related_name="allocations")
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="bank_allocations")
    purpose = models.CharField(max_length=16, choices=Purpose.choices)
    amount_minor = models.BigIntegerField()
    source = models.CharField(max_length=8, choices=[("auto", "Automatic"), ("manual", "Manual")])
    decision = models.ForeignKey(ReconciliationDecision, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    #: Reserved for the school fee ledger: the invoice or fee item this pays, once those exist.
    receivable_ref = models.CharField(max_length=80, blank=True)
    #: A newer decision replaces an allocation by marking it superseded - it is never deleted.
    superseded = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [models.CheckConstraint(condition=Q(amount_minor__gt=0), name="allocation_amount_positive")]


class BankAuditEvent(models.Model):
    """Who did what to a connection, and when. Written for every change and never edited.
    Details are scrubbed of anything that looks like a secret before they are stored."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    actor = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    connection = models.ForeignKey(BankConnection, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_events")
    kind = models.CharField(max_length=32)
    detail = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["school", "-at"])]


class BankWebhookEvent(models.Model):
    """Every provider callback that reached us, by payload hash: the same delivery twice is
    recognised and not processed twice."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=40)
    connection = models.ForeignKey(BankConnection, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    payload_hash = models.CharField(max_length=64)
    signature_valid = models.BooleanField(default=False)
    outcome = models.CharField(max_length=24, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "payload_hash"], name="unique_bank_webhook_payload"),
        ]


class SandboxFeedItem(models.Model):
    """Sandbox only: a synthetic transaction waiting on a sandbox connection's feed, standing in
    for what a real bank would report. Nothing else in the system reads this table."""

    id = models.BigAutoField(primary_key=True)
    connection = models.ForeignKey(BankConnection, on_delete=models.CASCADE, related_name="sandbox_feed")
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
