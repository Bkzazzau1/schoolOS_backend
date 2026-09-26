import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

from apps.academics.models import AcademicSession, AcademicTerm
from apps.bankconnect.constants import COLLECTION_PROVIDER_CODES
from apps.bankconnect.models import CollectionProviderConnection
from apps.receivables.models import Family, FamilyCollectionAccount
from apps.schools.models import Membership, School

from .constants import (
    AccountMode,
    ArrearsPolicy,
    BatchStatus,
    Eligibility,
    EligibilityPolicy,
    ExpiryKind,
    GenerationStatus,
    JobKind,
    JobStatus,
    OverrideScope,
    ReasonPolicy,
    ReuseScope,
    SettlementAction,
    SwitchPolicy,
    SwitchStatus,
)


class SchoolCollectionPolicy(models.Model):
    """The school's default collection policy: what every family's account does unless a session, term, batch or family says otherwise.
    One row per school, typed field by field - a policy is never a blob of JSON, so every choice is validated and can be shown."""

    school = models.OneToOneField(School, primary_key=True, on_delete=models.CASCADE, related_name="collection_policy")
    default_account_mode = models.CharField(max_length=8, choices=AccountMode.choices, default=AccountMode.STATIC)
    default_reuse_scope = models.CharField(max_length=20, choices=ReuseScope.choices, default=ReuseScope.UNTIL_REPLACED)
    #: How many terms or sessions, for the scopes that count them.
    default_reuse_count = models.PositiveSmallIntegerField(null=True, blank=True)
    default_reuse_until = models.DateField(null=True, blank=True)
    default_settlement_action = models.CharField(max_length=24, choices=SettlementAction.choices, default=SettlementAction.DORMANT_IMMEDIATELY)
    #: How long a settled account waits, in hours. Only the "wait, then ..." actions use it, and it has no default: the school chooses.
    default_grace_period_hours = models.PositiveIntegerField(null=True, blank=True)
    default_arrears_policy = models.CharField(max_length=20, choices=ArrearsPolicy.choices, default=ArrearsPolicy.CARRY_FORWARD)
    default_eligibility_policy = models.CharField(max_length=16, choices=EligibilityPolicy.choices, default=EligibilityPolicy.NEEDS_OVERRIDE)
    default_provider_switch_policy = models.CharField(max_length=20, choices=SwitchPolicy.choices, default=SwitchPolicy.RETIRE_WHEN_SETTLED)
    override_reason_policy = models.CharField(max_length=20, choices=ReasonPolicy.choices, default=ReasonPolicy.REQUIRED_SENSITIVE)
    updated_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "school collection policies"


class CollectionPolicyOverride(models.Model):
    """One layer over the school's default, for a session, a term, a batch or one family. It holds ONLY the fields that were
    explicitly changed (everything else keeps inheriting). An override is never edited or deleted: it is replaced or removed, and the
    old one stays as history with who, when and why."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    scope = models.CharField(max_length=8, choices=OverrideScope.choices)
    session = models.ForeignKey(AcademicSession, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    term = models.ForeignKey(AcademicTerm, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    batch = models.ForeignKey("smartcollect.CollectionGenerationBatch", null=True, blank=True, on_delete=models.PROTECT, related_name="policy_overrides")
    family = models.ForeignKey(Family, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    values = models.JSONField(default=dict)
    reason = models.CharField(max_length=300, blank=True)

    expiry_kind = models.CharField(max_length=16, choices=ExpiryKind.choices, default=ExpiryKind.UNTIL_REMOVED)
    #: For "until a date".
    expires_on = models.DateField(null=True, blank=True)
    #: For "end of term" / "end of session": which one, chosen when the override was made.
    expiry_term = models.ForeignKey(AcademicTerm, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    expiry_session = models.ForeignKey(AcademicSession, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    #: A one-time override is used up by the first account generated with it.
    consumed_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    removed_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    removed_at = models.DateTimeField(null=True, blank=True)
    removal_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(scope="session", session__isnull=False, term__isnull=True, batch__isnull=True, family__isnull=True)
                    | Q(scope="term", term__isnull=False, session__isnull=True, batch__isnull=True, family__isnull=True)
                    | Q(scope="batch", batch__isnull=False, session__isnull=True, term__isnull=True, family__isnull=True)
                    | Q(scope="family", family__isnull=False, session__isnull=True, term__isnull=True, batch__isnull=True)
                ),
                name="policy_override_has_exactly_its_own_target",
            ),
            # One live override per target: a new one replaces it (the old is marked removed and stays as history).
            models.UniqueConstraint(fields=["school", "session"], condition=Q(scope="session", removed_at__isnull=True), name="one_live_session_override"),
            models.UniqueConstraint(fields=["school", "term"], condition=Q(scope="term", removed_at__isnull=True), name="one_live_term_override"),
            models.UniqueConstraint(fields=["batch"], condition=Q(scope="batch", removed_at__isnull=True), name="one_live_batch_override"),
            models.UniqueConstraint(fields=["school", "family"], condition=Q(scope="family", removed_at__isnull=True), name="one_live_family_override"),
        ]
        indexes = [models.Index(fields=["school", "scope", "removed_at"])]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = CollectionPolicyOverride.objects.filter(pk=self.pk).values("values", "reason", "scope", "created_by_id").first()
            if stored and (stored["values"], stored["reason"], stored["scope"]) != (self.values, self.reason, self.scope):
                raise ValidationError("A policy override is never edited: replace it, and the old one stays as history.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A policy override is never deleted: remove it, and it stays as history.")


class CollectionAuditEvent(models.Model):
    """Who did what in Smart Money Collection, and when. Written for every material change and never edited or deleted. Details are
    scrubbed of anything that looks like a secret before they are stored."""

    #: A running number, so events that happen within the same instant still read in the order they happened.
    id = models.BigAutoField(primary_key=True)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    actor = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=40)
    object_type = models.CharField(max_length=40)
    object_id = models.CharField(max_length=64)
    detail = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["school", "-at"]), models.Index(fields=["school", "object_type", "object_id"])]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("A collection audit event is never edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A collection audit event is never deleted.")


class CollectionGenerationBatch(models.Model):
    """One run of "generate the collection accounts": a session and term, the school's ACTIVE provider, and the families chosen. It is
    prepared by a maker, approved by a different checker for exactly the snapshot they saw, and only then does anything reach the provider."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="collection_batches")
    session = models.ForeignKey(AcademicSession, on_delete=models.PROTECT, related_name="+")
    term = models.ForeignKey(AcademicTerm, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    #: The provider connection this batch will generate with: the school's active provider when it was made. It must still be the active
    #: provider when the batch is approved and when it is run.
    provider_connection = models.ForeignKey(CollectionProviderConnection, on_delete=models.PROTECT, related_name="collection_batches")
    #: What that connection was when the batch was made (a snapshot: the batch reads correctly whatever happens to the connection).
    provider = models.CharField(max_length=40)
    environment = models.CharField(max_length=8)
    title = models.CharField(max_length=120, blank=True)
    #: The batch-level policy the items were worked out under, with where each value came from.
    policy_snapshot = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=20, choices=BatchStatus.choices, default=BatchStatus.DRAFT)
    #: Rises every time the batch's substance changes. A screen sends the version it was looking at; a stale one is refused.
    version = models.PositiveIntegerField(default=1)
    #: Deterministic fingerprint of exactly what would be generated. Approval is for THIS hash.
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

    #: Totals over the SELECTED families, kept for lists and screens; the items are the truth.
    family_count = models.PositiveIntegerField(default=0)
    selected_count = models.PositiveIntegerField(default=0)
    total_previous_arrears_minor = models.BigIntegerField(default=0)
    total_current_due_minor = models.BigIntegerField(default=0)
    total_collection_minor = models.BigIntegerField(default=0)
    success_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    preview_built_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-prepared_at", "-id"]
        constraints = [
            # The checker is never the maker: enforced by the database, not only by the service.
            models.CheckConstraint(condition=~Q(approved_by=F("prepared_by")), name="checker_is_not_the_preparer"),
            models.CheckConstraint(condition=~Q(approved_by=F("submitted_by")), name="checker_is_not_the_submitter"),
            models.CheckConstraint(condition=Q(rejected_by__isnull=True) | ~Q(rejection_reason=""), name="a_rejection_has_a_reason"),
            models.CheckConstraint(
                condition=Q(approved_by__isnull=True) | (~Q(approved_snapshot_hash="") & Q(approved_version__isnull=False)),
                name="an_approval_is_for_a_snapshot",
            ),
        ]
        indexes = [models.Index(fields=["school", "status"]), models.Index(fields=["school", "-prepared_at"])]

    def save(self, *args, **kwargs):
        if self.session.school_id != self.school_id or self.provider_connection.school_id != self.school_id:
            raise ValidationError("A batch, its session and its provider connection must belong to the same school.")
        if self._state.adding and self.provider_connection.provider not in COLLECTION_PROVIDER_CODES:
            raise ValidationError("A collection batch can only be prepared for a Paystack or Monnify connection.")
        if self.term_id and self.term.session_id != self.session_id:
            raise ValidationError("A batch's term must be in its session.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A collection batch is never deleted: cancel it.")


class CollectionGenerationBatchItem(models.Model):
    """One family in a batch: the figures it was worked out with, whether it is selected, and how its account generation went."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(CollectionGenerationBatch, on_delete=models.PROTECT, related_name="items")
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    family = models.ForeignKey(Family, on_delete=models.PROTECT, related_name="+")
    selected = models.BooleanField(default=False)

    previous_arrears_minor = models.BigIntegerField(default=0)
    current_due_minor = models.BigIntegerField(default=0)
    credit_minor = models.BigIntegerField(default=0)
    #: What the account is asked to collect: the current due plus whatever of the previous balance the arrears policy carries.
    proposed_collection_minor = models.BigIntegerField(default=0)
    #: The earlier balances that could be carried, so a person choosing which to include sees exactly what each is.
    arrears_breakdown = models.JSONField(default=list, blank=True)
    custom_arrears_receivable_ids = models.JSONField(default=list, blank=True)
    arrears_policy = models.CharField(max_length=20, choices=ArrearsPolicy.choices, default=ArrearsPolicy.CARRY_FORWARD)

    eligibility_status = models.CharField(max_length=20, choices=Eligibility.choices, default=Eligibility.ELIGIBLE)
    eligibility_note = models.CharField(max_length=300, blank=True)
    #: An authorised person including a family the policy did not include. It changes nothing in the ledger: the arrears stay owed.
    eligibility_override = models.BooleanField(default=False)
    override_reason = models.CharField(max_length=300, blank=True)
    override_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    override_at = models.DateTimeField(null=True, blank=True)
    #: The checker's individual approval, where the school's policy asks for one per family.
    manual_approved_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    manual_approved_at = models.DateTimeField(null=True, blank=True)

    #: The policy that applies to THIS family in this batch, with where each value came from.
    policy_snapshot = models.JSONField(default=dict, blank=True)
    #: Fingerprint of the figures above, what the batch's hash is made of.
    fingerprint = models.CharField(max_length=64, blank=True)

    generation_status = models.CharField(max_length=12, choices=GenerationStatus.choices, default=GenerationStatus.SKIPPED)
    #: The same on every attempt of this family in this batch: what makes generation safe to repeat.
    idempotency_key = models.CharField(max_length=80, blank=True)
    #: Rises with each "retry failed", so a retry is a new job under the same idempotency key.
    retry_round = models.PositiveSmallIntegerField(default=0)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    provider_request_reference = models.CharField(max_length=120, blank=True)
    provider_account_reference = models.CharField(max_length=120, blank=True)
    #: The family's account this item generated (and, the other way round, `account.generation_items` says which batch made an account).
    account = models.ForeignKey(FamilyCollectionAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="generation_items")
    #: How far a provider call with several steps got (for example the customer it already made), kept even when the call failed.
    checkpoint = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    safe_error_message = models.CharField(max_length=300, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["family__display_name", "family__code"]
        constraints = [
            models.UniqueConstraint(fields=["batch", "family"], name="one_item_per_family_per_batch"),
            models.UniqueConstraint(fields=["school", "idempotency_key"], condition=~Q(idempotency_key=""), name="one_item_per_generation_key"),
            models.CheckConstraint(
                condition=~Q(generation_status="success") | ~Q(provider_account_reference=""), name="a_generated_item_names_its_provider_account",
            ),
            models.CheckConstraint(condition=~Q(manual_approved_by=F("override_by")) | Q(manual_approved_by__isnull=True), name="approver_did_not_override"),
        ]
        indexes = [models.Index(fields=["batch", "generation_status"]), models.Index(fields=["batch", "eligibility_status"])]

    def save(self, *args, **kwargs):
        if self.family.school_id != self.batch.school_id or self.school_id != self.batch.school_id:
            raise ValidationError("A batch item, its family and its batch must belong to the same school.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A batch item is never deleted.")


class CollectionBatchEvent(models.Model):
    """The history of a batch: every edit, submission, approval, rejection (with its reason), invalidation and retry. Never edited."""

    id = models.BigAutoField(primary_key=True)
    batch = models.ForeignKey(CollectionGenerationBatch, on_delete=models.PROTECT, related_name="events")
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


class ProviderJob(models.Model):
    """One call to a provider, waiting its turn. The batch (or the lifecycle) writes it in the same transaction as the decision, and a
    worker carries it out LATER, outside any database transaction: a provider is never called while a lock is held, and a call that
    dies half-way is picked up again (the connectors and the idempotency keys make repeating it safe)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=12, choices=JobKind.choices)
    status = models.CharField(max_length=10, choices=JobStatus.choices, default=JobStatus.QUEUED)
    item = models.ForeignKey(CollectionGenerationBatchItem, null=True, blank=True, on_delete=models.PROTECT, related_name="jobs")
    account = models.ForeignKey(FamilyCollectionAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
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


class ProviderSwitch(models.Model):
    """A change of the school's active collection provider. It is scheduled, becomes READY when its date has come and nothing stands in
    the way, and is applied only by a person's explicit act: SchoolOS never switches a school's provider on its own."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    from_connection = models.ForeignKey(CollectionProviderConnection, on_delete=models.PROTECT, related_name="+")
    to_connection = models.ForeignKey(CollectionProviderConnection, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=16, choices=SwitchStatus.choices, default=SwitchStatus.SCHEDULED)
    scheduled_for = models.DateTimeField()
    #: What happens to the accounts already made, as the school's policy said when this was scheduled.
    switch_policy = models.CharField(max_length=20, choices=SwitchPolicy.choices, default=SwitchPolicy.RETIRE_WHEN_SETTLED)
    #: The latest review (affected families and accounts, what is owed, readiness) and what still stands in the way.
    review = models.JSONField(default=dict, blank=True)
    blockers = models.JSONField(default=list, blank=True)
    note = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    applied_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    applied_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=300, blank=True)
    failure_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["school"], condition=Q(status__in=["scheduled", "ready_to_switch"]), name="one_open_provider_switch_per_school"),
            models.CheckConstraint(condition=~Q(from_connection=F("to_connection")), name="a_switch_changes_provider"),
        ]

    def save(self, *args, **kwargs):
        if self.from_connection.school_id != self.school_id or self.to_connection.school_id != self.school_id:
            raise ValidationError("A provider switch is between two of the school's own provider connections.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A provider switch is never deleted: cancel it.")


class FamilyPayerIdentity(models.Model):
    """The identity number some providers require of a family's payer (Monnify asks for a BVN or NIN before it will reserve an
    account). It is written and never read back: sealed by the vault, bound to this school and family, and only ever shown as "on file".
    It is used at the moment an account is made, and by nothing else."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    family = models.OneToOneField(Family, on_delete=models.PROTECT, related_name="payer_identity")
    sealed = models.BinaryField(blank=True, default=b"")
    has_bvn = models.BooleanField(default=False)
    has_nin = models.BooleanField(default=False)
    updated_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self.family.school_id != self.school_id:
            raise ValidationError("A payer identity and its family must belong to the same school.")
        super().save(*args, **kwargs)
