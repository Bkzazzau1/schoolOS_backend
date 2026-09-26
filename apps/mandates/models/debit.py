import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

from apps.academics.models import AcademicSession, AcademicTerm
from apps.receivables.models import Family, FamilyGuardian
from apps.schools.models import Membership, School

from ..constants import (
    DEFAULT_CURRENCY,
    BatchStatus,
    DebitOutcome,
    Eligibility,
    InstructionStatus,
    JobKind,
    JobStatus,
)
from .connection import MandateProviderConnection
from .mandate import DirectDebitMandate


class MandateDebitBatch(models.Model):
    """One run of "debit these families through their mandates": prepared by a maker, approved by a DIFFERENT checker for exactly the snapshot
    they saw, and only then sent to a provider, one instruction at a time, through the job queue.

    A batch never decides what a family owes. Each instruction's amount is worked out from the receivables ledger, can only be lowered by the
    maker, and is checked against the ledger again just before the provider is asked.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="mandate_debit_batches")
    title = models.CharField(max_length=120, blank=True)
    #: What the batch collects for: a session and (optionally) one of its terms. A debit only pays the charges in this scope.
    session = models.ForeignKey(AcademicSession, on_delete=models.PROTECT, related_name="+")
    term = models.ForeignKey(AcademicTerm, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=20, choices=BatchStatus.choices, default=BatchStatus.DRAFT)
    #: Rises every time the batch's substance changes. A screen sends the version it was looking at; a stale one is refused.
    version = models.PositiveIntegerField(default=1)
    #: Deterministic fingerprint of exactly what would be debited. Approval is for THIS hash.
    snapshot_hash = models.CharField(max_length=64, blank=True)
    approved_snapshot_hash = models.CharField(max_length=64, blank=True)
    approved_version = models.PositiveIntegerField(null=True, blank=True)

    prepared_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    prepared_at = models.DateTimeField(auto_now_add=True)
    submitted_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=500, blank=True)
    cancelled_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    started_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    #: Totals over the SELECTED families, kept for lists and screens; the instructions are the truth.
    family_count = models.PositiveIntegerField(default=0)
    total_items = models.PositiveIntegerField(default=0)
    total_outstanding_minor = models.BigIntegerField(default=0)
    total_amount_minor = models.BigIntegerField(default=0)
    success_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    preview_built_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-prepared_at", "-id"]
        constraints = [
            # The checker is never the maker: enforced by the database, not only by the service.
            models.CheckConstraint(condition=~Q(approved_by=F("prepared_by")), name="debit_checker_is_not_the_preparer"),
            models.CheckConstraint(condition=~Q(approved_by=F("submitted_by")), name="debit_checker_is_not_the_submitter"),
            models.CheckConstraint(condition=Q(rejected_by__isnull=True) | ~Q(rejection_reason=""), name="a_debit_rejection_has_a_reason"),
            models.CheckConstraint(
                condition=Q(approved_by__isnull=True) | (~Q(approved_snapshot_hash="") & Q(approved_version__isnull=False)),
                name="a_debit_approval_is_for_a_snapshot",
            ),
        ]
        indexes = [models.Index(fields=["school", "status"]), models.Index(fields=["school", "-prepared_at"])]

    def save(self, *args, **kwargs):
        if self.session.school_id != self.school_id:
            raise ValidationError("A batch and its session must belong to the same school.")
        if self.term_id and self.term.session_id != self.session_id:
            raise ValidationError("A batch's term must be in its session.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A debit batch is never deleted: cancel it.")


class MandateBatchEvent(models.Model):
    """The history of a batch: every edit, submission, approval, rejection (with its reason), invalidation and retry. Never edited."""

    id = models.BigAutoField(primary_key=True)
    batch = models.ForeignKey(MandateDebitBatch, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=32)
    version = models.PositiveIntegerField(default=1)
    detail = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("A batch event is never edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A batch event is never deleted.")


class MandateDebitInstruction(models.Model):
    """One family in a batch: the mandate that would be used, what the ledger says is owed, what is proposed and how each charge would be paid,
    whether it is selected, and how the debit went."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(MandateDebitBatch, on_delete=models.PROTECT, related_name="items")
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    family = models.ForeignKey(Family, on_delete=models.PROTECT, related_name="+")
    payer = models.ForeignKey(FamilyGuardian, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    #: The mandate this would debit through (empty for a family that has none), and what it was when the preview was built.
    mandate = models.ForeignKey(DirectDebitMandate, null=True, blank=True, on_delete=models.PROTECT, related_name="debit_items")
    provider_connection = models.ForeignKey(MandateProviderConnection, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    provider = models.CharField(max_length=40, blank=True)
    mandate_status = models.CharField(max_length=24, blank=True)
    selected = models.BooleanField(default=False)

    #: What the family owes in this batch's scope (from the ledger), what could be collected of it, the mandate's own limit and what is proposed.
    outstanding_minor = models.BigIntegerField(default=0)
    eligible_minor = models.BigIntegerField(default=0)
    maximum_minor = models.BigIntegerField(null=True, blank=True)
    proposed_debit_minor = models.BigIntegerField(default=0)
    #: The maker lowered the amount from what the ledger allows. It can only ever be lowered.
    amount_adjusted = models.BooleanField(default=False)
    #: How the proposed debit would pay the family's charges, in payment order: `[{"receivableId", "amountMinor"}]`.
    receivable_allocation = models.JSONField(default=list, blank=True)
    eligibility_status = models.CharField(max_length=24, choices=Eligibility.choices, default=Eligibility.ELIGIBLE)
    eligibility_note = models.CharField(max_length=300, blank=True)
    #: Fingerprint of everything above that matters: what the batch's hash is made of.
    fingerprint = models.CharField(max_length=64, blank=True)

    status = models.CharField(max_length=12, choices=InstructionStatus.choices, default=InstructionStatus.SKIPPED)
    #: The same on every attempt of this family in this batch: what makes the debit safe to repeat and safe to look up.
    idempotency_key = models.CharField(max_length=80, blank=True)
    #: The reference the provider is given for the debit (the same on every attempt). Numeric, derived from the key.
    request_ref = models.CharField(max_length=40, blank=True)
    #: Rises with each "retry failed", so a retry is a new job under the same reference.
    retry_round = models.PositiveSmallIntegerField(default=0)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    provider_transaction_reference = models.CharField(max_length=120, blank=True)
    provider_status = models.CharField(max_length=120, blank=True)
    provider_status_code = models.CharField(max_length=20, blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    safe_error_message = models.CharField(max_length=300, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["family__display_name", "family__code"]
        constraints = [
            models.UniqueConstraint(fields=["batch", "family"], name="one_debit_item_per_family_per_batch"),
            models.UniqueConstraint(fields=["school", "idempotency_key"], condition=~Q(idempotency_key=""), name="one_debit_item_per_idempotency_key"),
            models.UniqueConstraint(fields=["provider_connection", "request_ref"], condition=~Q(request_ref=""), name="one_debit_item_per_provider_request"),
            models.UniqueConstraint(
                fields=["provider_connection", "provider_transaction_reference"], condition=~Q(provider_transaction_reference=""),
                name="one_debit_item_per_provider_transaction",
            ),
            # A debit can only be chosen for a family that has a mandate, and can never be more than the ledger allowed or the mandate's limit.
            models.CheckConstraint(condition=Q(selected=False) | Q(mandate__isnull=False), name="a_selected_debit_has_a_mandate"),
            models.CheckConstraint(condition=Q(proposed_debit_minor__gte=0) & Q(proposed_debit_minor__lte=F("eligible_minor")), name="a_debit_never_exceeds_what_is_owed"),
            models.CheckConstraint(condition=Q(maximum_minor__isnull=True) | Q(proposed_debit_minor__lte=F("maximum_minor")), name="a_debit_never_exceeds_the_mandate_limit"),
            models.CheckConstraint(condition=~Q(status="success") | (~Q(request_ref="") & Q(proposed_debit_minor__gt=0)), name="a_successful_debit_has_a_reference_and_an_amount"),
        ]
        indexes = [models.Index(fields=["batch", "status"]), models.Index(fields=["batch", "eligibility_status"])]

    def save(self, *args, **kwargs):
        if self.family.school_id != self.batch.school_id or self.school_id != self.batch.school_id:
            raise ValidationError("A debit item, its family and its batch must belong to the same school.")
        if self.mandate_id and (self.mandate.school_id != self.school_id or self.mandate.family_id != self.family_id):
            raise ValidationError("A debit item can only use a mandate of its own family.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A debit item is never deleted.")


class MandateTransaction(models.Model):
    """One debit as the provider reports it, in SchoolOS's own shape - whichever provider it came from.

    It is the bridge to the receivables ledger: only a confirmed (`success`) debit is ever put towards the family's charges, and only once
    (`payment` is set exactly when that happened). A reversal or refund does not delete anything: it takes the payment back out of the ledger
    and is recorded here.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="mandate_transactions")
    family = models.ForeignKey(Family, on_delete=models.PROTECT, related_name="+")
    payer = models.ForeignKey(FamilyGuardian, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    mandate = models.ForeignKey(DirectDebitMandate, on_delete=models.PROTECT, related_name="transactions")
    #: One per attempt-round of an instruction: a deliberate retry after a failure is a new request with a new reference.
    instruction = models.ForeignKey(MandateDebitInstruction, on_delete=models.PROTECT, related_name="transactions")
    provider_connection = models.ForeignKey(MandateProviderConnection, on_delete=models.PROTECT, related_name="+")
    provider = models.CharField(max_length=40)
    request_ref = models.CharField(max_length=40)
    provider_transaction_reference = models.CharField(max_length=120, blank=True)
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    status = models.CharField(max_length=10, choices=DebitOutcome.choices, default=DebitOutcome.PENDING)
    provider_status = models.CharField(max_length=120, blank=True)
    provider_status_code = models.CharField(max_length=20, blank=True)
    failure_code = models.CharField(max_length=40, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    reversed_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    #: The receivables payment this debit became, once it was confirmed (see `BankTransaction`). Set once, never replaced.
    payment = models.OneToOneField("bankconnect.BankTransaction", null=True, blank=True, on_delete=models.PROTECT, related_name="mandate_transaction")
    provider_meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["provider_connection", "request_ref"], name="one_mandate_transaction_per_request"),
            models.UniqueConstraint(
                fields=["provider_connection", "provider_transaction_reference"], condition=~Q(provider_transaction_reference=""),
                name="one_mandate_transaction_per_provider_reference",
            ),
            models.CheckConstraint(condition=Q(amount_minor__gt=0), name="mandate_transaction_amount_positive"),
            # Only a debit the provider confirmed can ever have become a payment.
            models.CheckConstraint(
                condition=Q(payment__isnull=True) | Q(status__in=[DebitOutcome.SUCCESS, DebitOutcome.REVERSED, DebitOutcome.REFUNDED]),
                name="only_a_confirmed_debit_is_a_payment",
            ),
        ]
        indexes = [models.Index(fields=["school", "status"]), models.Index(fields=["school", "family"])]

    def save(self, *args, **kwargs):
        if self.family.school_id != self.school_id or self.mandate.school_id != self.school_id or self.provider_connection.school_id != self.school_id:
            raise ValidationError("A mandate transaction, its family, its mandate and its provider connection must belong to the same school.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A mandate transaction is never deleted.")


class MandateProviderJob(models.Model):
    """One call to a provider, waiting its turn. The batch writes it in the same transaction as the decision to debit, and a worker carries it
    out LATER, outside any database transaction: a provider is never called while a lock is held, and a call that dies half-way is picked up
    again - but only after asking the provider what happened to the first one."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=10, choices=JobKind.choices)
    status = models.CharField(max_length=10, choices=JobStatus.choices, default=JobStatus.QUEUED)
    instruction = models.ForeignKey(MandateDebitInstruction, on_delete=models.PROTECT, related_name="jobs")
    idempotency_key = models.CharField(max_length=120, unique=True)
    run_after = models.DateTimeField()
    lease_until = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error_code = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["run_after", "created_at"]
        indexes = [models.Index(fields=["status", "run_after"])]
