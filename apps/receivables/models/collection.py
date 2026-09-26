import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.academics.models import AcademicSession, AcademicTerm
from apps.bankconnect.constants import COLLECTION_PROVIDER_CODES
from apps.schools.models import Membership, School

from ..constants import DEFAULT_CURRENCY
from .family import Family


class AccountStatus(models.TextChoices):
    #: Being set up with the provider. Not yet able to receive.
    PROVISIONING = "provisioning", "Being set up"
    #: The family owes money, so payments to this account are expected.
    ACTIVE = "active", "Active"
    #: What this account collects for is settled. The school's policy decides what happens next (close, rest, wait, or a person acts).
    SETTLED = "settled", "Settled"
    #: Settled, and waiting out the school's grace period before the policy's next step.
    GRACE = "grace", "In grace period"
    #: The family owes nothing right now. The account is kept (not closed or deleted) and wakes up again
    #: when new fees are published.
    DORMANT = "dormant", "Dormant"
    #: Stopped by a person, or by a provider problem. Only a person brings it back.
    SUSPENDED = "suspended", "Suspended"
    #: Being retired at the provider (the provider call is in flight or waiting to be retried).
    CLOSING = "closing", "Closing"
    CLOSED = "closed", "Closed"
    #: The provider could not make the account. It never received anything and can be replaced.
    FAILED = "failed", "Failed"


#: An account that still counts: a family has at most ONE of these per school. A closed or failed account is history.
LIVE_STATUSES = (
    AccountStatus.PROVISIONING, AccountStatus.ACTIVE, AccountStatus.SETTLED, AccountStatus.GRACE,
    AccountStatus.DORMANT, AccountStatus.SUSPENDED, AccountStatus.CLOSING,
)
#: Statuses that end an account's life.
ENDED_STATUSES = (AccountStatus.CLOSED, AccountStatus.FAILED)


class AccountOrigin(models.TextChoices):
    #: Made by the school's active collection provider through Smart Money Collection.
    PROVIDER = "provider", "Provider-generated"
    #: Recorded by hand, before Smart Money Collection or by an owner-level administrative act. Never the normal journey.
    LEGACY_MANUAL = "legacy_manual", "Recorded by hand (legacy)"


class AccountMode(models.TextChoices):
    #: A reusable account: kept for the scope the school's policy says (a term, a session, until a date, indefinitely...).
    STATIC = "static", "Static"
    #: Made for one collection scope and its amount, then retired according to policy.
    DYNAMIC = "dynamic", "Dynamic"


class FamilyCollectionAccount(models.Model):
    """The receiving identity ONE FAMILY pays into, underneath the school's collection arrangement with a
    provider. It is not a `BankConnection` (which is the SCHOOL'S own account being connected to SchoolOS).

    The core knows nothing about how a provider implements the account. Whether "dormant" means the
    provider rejects transfers, or SchoolOS simply flags anything that arrives, is a provider adapter's
    decision; here it is only a status that follows what the family owes.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    family = models.ForeignKey(Family, on_delete=models.PROTECT, related_name="collection_accounts")
    provider = models.CharField(max_length=40)
    #: The school's provider connection this account was made under, and under which its payments arrive. Always set for an account
    #: made by Smart Money Collection; empty only for an account recorded by hand (legacy).
    connection = models.ForeignKey("bankconnect.CollectionProviderConnection", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    origin = models.CharField(max_length=14, choices=AccountOrigin.choices, default=AccountOrigin.PROVIDER)
    account_mode = models.CharField(max_length=8, choices=AccountMode.choices, default=AccountMode.STATIC)
    #: What the account collects for. Empty scope means it is not tied to one session or term.
    scope_session = models.ForeignKey(AcademicSession, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    scope_term = models.ForeignKey(AcademicTerm, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    valid_from = models.DateField(null=True, blank=True)
    #: The last day a static account is reused for (empty: no end date - see the policy's reuse scope).
    valid_until = models.DateField(null=True, blank=True)
    #: How long the school's policy said this account is reused, as it was when the account was made (one term, selected terms, one
    #: session, multiple sessions, until a date, indefinitely, until replaced), and how many terms or sessions where that applies.
    #: Recorded on the account so a later change of policy never quietly changes what an account already made was promised.
    reuse_scope = models.CharField(max_length=20, blank=True)
    reuse_count = models.PositiveSmallIntegerField(null=True, blank=True)
    #: While the account is settled and waiting out the school's grace period: when the wait ends and what follows it ("dormant" or "close").
    grace_until = models.DateTimeField(null=True, blank=True)
    after_grace = models.CharField(max_length=8, blank=True)
    #: What the account was made to collect, in minor units, when it was made (the family's collection target). A record of what
    #: was decided then; it is never how much the family owes (the ledger says that).
    collection_target_minor = models.BigIntegerField(null=True, blank=True)
    #: The same on every retry of one account's generation: what stops a retry, a double click or a worker race making a second account.
    idempotency_key = models.CharField(max_length=80, blank=True)
    #: The provider's own id for the account. Kept for life so the same account can be reused.
    external_account_ref = models.CharField(max_length=120, blank=True)
    #: What a payer types or transfers to, and what `BankTransaction.receiving_account_ref` carries.
    account_number = models.CharField(max_length=40, blank=True)
    account_name = models.CharField(max_length=200, blank=True)
    bank_name = models.CharField(max_length=120, blank=True)
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    status = models.CharField(max_length=14, choices=AccountStatus.choices, default=AccountStatus.PROVISIONING)
    activated_at = models.DateTimeField(null=True, blank=True)
    dormant_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    close_reason = models.CharField(max_length=200, blank=True)
    status_changed_at = models.DateTimeField(null=True, blank=True)
    #: Extra facts a payer is shown with the number - what this provider needs them to know, as
    #: `[{"label", "value"}]` (a payment reference, a sort code). Public: the family sees exactly this.
    public_details = models.JSONField(default=list, blank=True)
    #: Safe-to-keep facts from the provider. Never credentials, and never shown to a family.
    provider_meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["school", "provider", "account_number"], condition=~Q(account_number=""), name="one_family_account_per_number"),
            models.UniqueConstraint(fields=["school", "provider", "external_account_ref"], condition=~Q(external_account_ref=""), name="one_family_account_per_provider_ref"),
            # At most ONE live collection account per family per school, whatever the provider: the accounting identity is the family.
            models.UniqueConstraint(
                fields=["school", "family"], condition=~Q(status__in=["closed", "failed"]), name="one_live_collection_account_per_family_per_school",
            ),
            models.UniqueConstraint(
                fields=["school", "idempotency_key"], condition=~Q(idempotency_key=""), name="one_collection_account_per_generation_key",
            ),
            # An account made through Smart Money Collection always belongs to one of the school's provider connections.
            models.CheckConstraint(condition=Q(origin="legacy_manual") | Q(connection__isnull=False), name="provider_account_has_a_connection"),
        ]
        indexes = [models.Index(fields=["school", "family", "status"])]

    def save(self, *args, **kwargs):
        if self.family.school_id != self.school_id:
            raise ValidationError("A collection account and its family must belong to the same school.")
        if self.connection_id and self.connection.school_id != self.school_id:
            raise ValidationError("A collection account and its provider connection must belong to the same school.")
        if self.scope_term_id and self.scope_session_id and self.scope_term.session_id != self.scope_session_id:
            raise ValidationError("A collection account's term must be in its session.")
        if self.origin == AccountOrigin.PROVIDER and not self.connection_id:
            raise ValidationError("A provider-generated collection account belongs to a provider connection.")
        if self.origin == AccountOrigin.PROVIDER and self._state.adding and (
            self.provider not in COLLECTION_PROVIDER_CODES or self.connection.provider not in COLLECTION_PROVIDER_CODES
        ):
            raise ValidationError("A family's collection account can only be made by a Paystack or Monnify connection.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A collection account is never deleted: close it.")


class StatementStatus(models.TextChoices):
    ISSUED = "issued", "Issued"
    VOID = "void", "Void"


class FamilyStatement(models.Model):
    """A statement a school issued to a family for a session or term: its number, when, and by whom.

    The figures on a statement are ALWAYS worked out from the ledger when it is read. `snapshot` keeps a
    small record of what the totals were on the day it was issued, for reference only: it is never the
    source of truth for money, so it cannot drift from the ledger.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    family = models.ForeignKey(Family, on_delete=models.PROTECT, related_name="statements")
    session = models.ForeignKey(AcademicSession, on_delete=models.PROTECT, related_name="+")
    term = models.ForeignKey(AcademicTerm, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    number = models.CharField(max_length=40)
    status = models.CharField(max_length=8, choices=StatementStatus.choices, default=StatementStatus.ISSUED)
    issued_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    issued_at = models.DateTimeField(auto_now_add=True)
    snapshot = models.JSONField(default=dict, blank=True)
    #: A statement is never edited or deleted: one issued in error is VOIDED, with who and why, and stays on record.
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    void_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-issued_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["school", "number"], name="unique_statement_number_per_school")]
        indexes = [models.Index(fields=["school", "family", "-issued_at"])]

    def save(self, *args, **kwargs):
        if self.family.school_id != self.school_id or self.session.school_id != self.school_id:
            raise ValidationError("A statement, its family and its session must belong to one school.")
        super().save(*args, **kwargs)
