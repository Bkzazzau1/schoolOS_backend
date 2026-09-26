import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.schools.models import Membership, School
from apps.students.models import Student

from .constants import (
    COLLECTION_PROVIDER_CODES,
    DEFAULT_CURRENCY,
    SMART_PROVIDERS,
    ConnectionStatus,
    ConnectionType,
    Direction,
    Environment,
    Purpose,
    ReconStatus,
    WebhookStatus,
)


class CollectionProviderConnection(models.Model):
    """The SCHOOL'S OWN relationship with a collection provider (Paystack or Monnify), as SchoolOS holds it.

    The school onboards with the provider directly, receives its own credentials and enters them here. SchoolOS uses them
    to create family collection accounts and to read the provider's signed payment events; the provider moves and settles
    the money, and SchoolOS never receives or holds it. A school may connect several providers but exactly ONE is the
    active collection provider (`is_active_provider`) at a time: only that one generates new family accounts.

    Everything here is safe to show a signed-in finance user. The credential itself exists only as `sealed_credentials`
    (see vault.py) and is never serialised anywhere. It does NOT need, and never asks for, the school's settlement bank account.

    The bank-account columns further down (`bank_name`, `account_name`, `account_mask`, `account_fingerprint`, `purpose`,
    `sync_cursor`, `last_synced_at`) belong to the earlier "connect a bank account" model. They are kept, empty for a
    provider connection, so older rows and the payments recorded against them are not disturbed.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="bank_connections")
    provider = models.CharField(max_length=40)
    connection_type = models.CharField(max_length=24, choices=ConnectionType.choices, default=ConnectionType.COLLECTION_PROVIDER)
    environment = models.CharField(max_length=8, choices=Environment.choices, default=Environment.LIVE)

    #: What the provider reports about the school's merchant profile, once verified: a name to recognise it by and a
    #: masked public identifier. Never a secret.
    merchant_name = models.CharField(max_length=200, blank=True)
    merchant_reference = models.CharField(max_length=60, blank=True)
    #: Non-secret choices the school made for this provider (for example the bank a Paystack account is issued from).
    provider_settings = models.JSONField(default=dict, blank=True)
    #: The school's own name for the connection.
    label = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=16, choices=ConnectionStatus.choices, default=ConnectionStatus.PENDING)
    #: True for exactly one live connection of a school (a database rule): the provider that generates new family accounts.
    is_active_provider = models.BooleanField(default=False)

    sealed_credentials = models.BinaryField(blank=True, default=b"")
    token_expires_at = models.DateTimeField(null=True, blank=True)
    #: Only ever a hash, for looking the connection up from the address the provider calls. (The address itself is kept
    #: inside the sealed credential so an authorised person can be shown it again.)
    webhook_token_hash = models.CharField(max_length=64, blank=True, db_index=True)
    webhook_status = models.CharField(max_length=16, choices=WebhookStatus.choices, default=WebhookStatus.NOT_CONFIGURED)
    #: When the first verified event arrived: the webhook is not called active before that.
    webhook_confirmed_at = models.DateTimeField(null=True, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)

    #: A short code, never a provider's own message: those can echo what was sent.
    last_error_code = models.CharField(max_length=40, blank=True)
    provider_meta = models.JSONField(default=dict, blank=True)
    is_sandbox = models.BooleanField(default=False)

    # -- the earlier bank-account model (empty for a provider connection) ---------------------------
    bank_name = models.CharField(max_length=120, blank=True)
    account_name = models.CharField(max_length=200, blank=True)
    account_mask = models.CharField(max_length=16, blank=True)
    account_fingerprint = models.CharField(max_length=64, blank=True)
    purpose = models.CharField(max_length=16, choices=Purpose.choices, default=Purpose.GENERAL)
    sync_cursor = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    disconnected_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            # One live connection per collection provider per school (a replaced credential is the same connection).
            models.UniqueConstraint(
                fields=["school", "provider"],
                condition=Q(provider__in=SMART_PROVIDERS) & ~Q(status=ConnectionStatus.REVOKED),
                name="one_live_provider_connection_per_school",
            ),
            # Exactly one active collection provider per school, enforced by the database.
            models.UniqueConstraint(
                fields=["school"], condition=Q(is_active_provider=True), name="one_active_collection_provider_per_school",
            ),
            # Only a provider Smart Money Collection offers can be the active one. Remita (reserved for Mandates / Direct Debit) and any
            # provider left by the earlier bank-account model never can, whatever an application bug might try.
            models.CheckConstraint(
                condition=Q(is_active_provider=False) | Q(provider__in=COLLECTION_PROVIDER_CODES),
                name="active_provider_is_a_collection_provider",
            ),
            # The active provider is one that is still in use: a blip (needs re-authorising, an error) does not take the
            # designation away, but a disabled, pending or disconnected connection cannot hold it.
            models.CheckConstraint(
                condition=Q(is_active_provider=False)
                | Q(status__in=[ConnectionStatus.CONNECTED, ConnectionStatus.NEEDS_REAUTH, ConnectionStatus.ERROR]),
                name="active_provider_is_in_use",
            ),
        ]
        indexes = [models.Index(fields=["school", "status"])]

    def __str__(self):
        return f"{self.provider} {self.environment} ({self.school_id})"


#: The name the model went by before Smart Money Collection. Kept so code and tests that still say `BankConnection` keep working.
BankConnection = CollectionProviderConnection


class BankTransaction(models.Model):
    """One movement on a connected account, in SchoolOS's own canonical shape - whichever bank
    or provider it came from."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="bank_transactions")
    #: PROTECT: a connection with history is disconnected, never deleted.
    connection = models.ForeignKey(CollectionProviderConnection, on_delete=models.PROTECT, related_name="transactions")

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
    #: The account the money was paid INTO, when the provider says which - for a family collection account
    #: this is what identifies the family with certainty. Empty when the provider does not say.
    receiving_account_ref = models.CharField(max_length=200, blank=True)
    #: The family this payment belongs to when that is known for certain (see `receiving_account_ref`).
    #: A guess never sets this: fuzzy matching only ever suggests a student.
    family = models.ForeignKey("receivables.Family", null=True, blank=True, on_delete=models.PROTECT, related_name="bank_transactions")
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
            models.Index(fields=["school", "provider", "receiving_account_ref"]),
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
    #: The charge this money pays, once the school's fee ledger has one for the student. Empty for the older
    #: student-level allocation, which says only who the money is for and what it was paid for.
    receivable = models.ForeignKey("receivables.StudentReceivable", null=True, blank=True, on_delete=models.PROTECT, related_name="allocations")
    family = models.ForeignKey("receivables.Family", null=True, blank=True, on_delete=models.PROTECT, related_name="allocations")
    #: Older free-text reference, kept so nothing stored before the ledger existed is lost.
    receivable_ref = models.CharField(max_length=80, blank=True)
    #: A newer decision replaces an allocation by marking it superseded - it is never deleted.
    superseded = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount_minor__gt=0), name="allocation_amount_positive"),
            models.CheckConstraint(condition=Q(receivable__isnull=True) | Q(family__isnull=False), name="receivable_allocation_has_a_family"),
        ]
        indexes = [models.Index(fields=["receivable", "superseded"])]


class BankAuditEvent(models.Model):
    """Who did what to a connection, and when. Written for every change and never edited.
    Details are scrubbed of anything that looks like a secret before they are stored."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    actor = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    connection = models.ForeignKey(CollectionProviderConnection, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_events")
    kind = models.CharField(max_length=32)
    detail = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["school", "-at"])]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("A connection audit event is never edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A connection audit event is never deleted.")


class BankWebhookEvent(models.Model):
    """Every provider callback that reached us, by payload hash: the same delivery twice is
    recognised and not processed twice."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=40)
    connection = models.ForeignKey(CollectionProviderConnection, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
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
    connection = models.ForeignKey(CollectionProviderConnection, on_delete=models.CASCADE, related_name="sandbox_feed")
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class SandboxProviderAccount(models.Model):
    """Sandbox only: the provider's side of a family collection account, standing in for what Paystack or Monnify would
    keep. It is what lets the whole path (provision, retire, look up, receive a payment) be exercised without any real provider.
    Nothing else in the system reads this table."""

    id = models.BigAutoField(primary_key=True)
    connection = models.ForeignKey(CollectionProviderConnection, on_delete=models.CASCADE, related_name="sandbox_accounts")
    reference = models.CharField(max_length=80)
    account_number = models.CharField(max_length=20)
    status = models.CharField(max_length=10, default="active")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["connection", "reference"], name="unique_sandbox_account_reference")]
