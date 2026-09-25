import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.schools.models import Membership, School
from apps.students.models import Student


class BadDebtStatus(models.TextChoices):
    """A single classification's own progression, private to the classifying
    school. Only BAD_DEBT can ever be published to TransferVerify (a separate,
    explicit, Proprietor-only action - see apps.transferverify.services);
    reaching this status never publishes anything by itself."""

    OUTSTANDING = "outstanding", "Outstanding"
    RECOVERY_IN_PROGRESS = "recovery_in_progress", "Recovery in progress"
    BAD_DEBT = "bad_debt", "Bad debt / unresolved obligation"
    RESOLVED = "resolved", "Resolved"


class PublicationReason(models.TextChoices):
    """Factual, neutral reasons a Proprietor may give for publishing a case -
    never an accusation. Matches the wording the brief itself specifies."""

    WITHDREW_WITHOUT_CLEARANCE = "withdrew_without_clearance", "Withdrew without financial clearance"
    GUARDIAN_UNREACHABLE = "guardian_unreachable", "Guardian unreachable after recovery attempts"
    ARRANGEMENT_DEFAULTED = "arrangement_defaulted", "Payment arrangement defaulted"
    UNRESOLVED_AFTER_WITHDRAWAL = "unresolved_after_withdrawal", "Unresolved balance after withdrawal"
    TRANSFER_SUSPECTED = "transfer_suspected", "Transfer suspected while balance remains"
    OTHER = "other", "Other documented reason"


class BadDebtClassification(models.Model):
    """One school's own internal record of an unresolved student-account
    obligation. This is the source school's private data: nothing here is
    visible to any other school, or discoverable through TransferVerify,
    until a Proprietor takes the separate "Publish to TransferVerify" action
    on a BAD_DEBT-status record (a later phase; see the platform-level
    TransferAlert).

    SchoolOS does not decide that a debt is bad - the Proprietor, or someone
    they specifically authorized (see apps.owner.jobs, duty
    "finance.bad_debt_classification"), does. This record exists to make that
    human decision auditable: who classified it, when, why, and what the
    amount was understood to be at the time.

    There is currently no canonical Student-finance ledger anywhere in
    SchoolOS (fee structures/payments/balances are still local-only demo
    data on the Flutter client with no backend behind them), so
    outstanding_amount_minor is a snapshot this record owns, not a reference
    to a ledger row that does not yet exist. current_canonical_balance_minor
    stays null until a real Finance backend exists to refresh it from - it
    is never invented.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="bad_debt_classifications")
    external_id = models.CharField(max_length=128)
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="bad_debt_classifications")

    status = models.CharField(max_length=24, choices=BadDebtStatus.choices, default=BadDebtStatus.OUTSTANDING)

    #: Snapshots, in kobo (minor units), matching the naming convention
    #: apps.billing already uses (*_minor) for money fields.
    outstanding_amount_minor = models.PositiveIntegerField()
    current_canonical_balance_minor = models.PositiveIntegerField(null=True, blank=True)

    reason = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    evidence_reference = models.CharField(max_length=200, blank=True)

    classified_by = models.ForeignKey(
        Membership, on_delete=models.PROTECT, related_name="classified_bad_debts"
    )
    classified_at = models.DateTimeField()
    last_updated_by = models.ForeignKey(
        Membership, null=True, blank=True, on_delete=models.PROTECT, related_name="updated_bad_debts"
    )

    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        Membership, null=True, blank=True, on_delete=models.PROTECT, related_name="resolved_bad_debts"
    )
    resolution_note = models.TextField(blank=True)

    #: Publication is deliberately kept on this record rather than a separate
    #: platform-level TransferAlert for now: a real TransferAlert must
    #: reference a NetworkStudentIdentity, which does not exist until the
    #: cross-school discovery phase is built. Until then, "published" only
    #: proves the authority chain itself (Proprietor-only, explicit
    #: confirmation, audited) - it does not yet make anything visible to any
    #: other school, because no cross-school lookup exists yet to find it.
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        Membership, null=True, blank=True, on_delete=models.PROTECT, related_name="published_bad_debts"
    )
    publication_reason = models.CharField(max_length=32, choices=PublicationReason.choices, blank=True)
    publication_note = models.TextField(blank=True)
    #: Reserved for the association-scope phase - always empty until then.
    association_scope = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def is_published(self) -> bool:
        return self.published_at is not None

    class Meta:
        ordering = ["-classified_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["school", "external_id"], name="bad_debt_external_uq"),
            # A student can have at most one classification open at a time -
            # resolve (or, later, publish and let the network case carry the
            # thread) before starting a new one, so history never forks.
            models.UniqueConstraint(
                fields=["student"],
                condition=Q(status__in=[BadDebtStatus.OUTSTANDING, BadDebtStatus.RECOVERY_IN_PROGRESS, BadDebtStatus.BAD_DEBT]),
                name="bad_debt_one_open_per_student",
            ),
        ]
        indexes = [
            models.Index(fields=["school", "status"], name="bad_debt_school_status_idx"),
        ]

    def __str__(self):
        return f"{self.student} · {self.get_status_display()}"


class BadDebtEvent(models.Model):
    """Append-only history for one classification - created, status changes,
    edits, resolution. Never edited once written, the same pattern
    apps.access.AccessChange and apps.cbt.CbtEvent already use."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    classification = models.ForeignKey(BadDebtClassification, on_delete=models.PROTECT, related_name="events")
    revision = models.PositiveIntegerField()
    action = models.CharField(max_length=32)
    actor_membership = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="bad_debt_events")
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(fields=["classification", "revision"], name="bad_debt_event_revision_uq"),
        ]


# --- The association layer ---------------------------------------------
#
# Platform-level (no school FK on the association itself) - deliberately not
# apps.organizations.Organization, which is the commercial SaaS billing
# account above one or more schools and carries no cooperation/data-sharing
# meaning. A SchoolProprietorAssociation is a separate, real-world network
# (for example a state private-schools association) whose member Proprietors
# have agreed to cooperate on TransferVerify - a school joins one only by an
# explicit request an association administrator approves, never implicitly.


class AssociationStatus(models.TextChoices):
    PENDING_VERIFICATION = "pending_verification", "Pending verification"
    ACTIVE = "active", "Active"
    SUSPENDED = "suspended", "Suspended"


class SchoolProprietorAssociation(models.Model):
    """A verified network of schools that may publish and discover
    TransferVerify cases with each other. Membership is per-school (see
    SchoolAssociationMembership), never automatic and never transitive."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, unique=True)
    registration_reference = models.CharField(max_length=200, blank=True)
    geographic_scope = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=24, choices=AssociationStatus.choices, default=AssociationStatus.PENDING_VERIFICATION)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    @property
    def is_open_for_membership(self) -> bool:
        return self.status == AssociationStatus.ACTIVE

    def __str__(self):
        return self.name


class AssociationMembershipStatus(models.TextChoices):
    PENDING = "pending", "Pending approval"
    ACTIVE = "active", "Active"
    SUSPENDED = "suspended", "Suspended"
    EXITED = "exited", "Exited"


class SchoolAssociationMembership(models.Model):
    """One school's own membership in one association. A school may hold
    several of these across different associations at once - publishing a
    TransferVerify case requires the Proprietor to pick specific active
    memberships explicitly (see BadDebtClassification.association_scope),
    never all of them by default."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    association = models.ForeignKey(SchoolProprietorAssociation, on_delete=models.CASCADE, related_name="school_memberships")
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="association_memberships")
    requested_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="requested_association_memberships")
    status = models.CharField(max_length=16, choices=AssociationMembershipStatus.choices, default=AssociationMembershipStatus.PENDING)
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        "AssociationAdministrator", null=True, blank=True, on_delete=models.SET_NULL, related_name="decided_memberships"
    )
    decision_note = models.TextField(blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    exited_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            models.UniqueConstraint(fields=["association", "school"], name="one_membership_per_school_per_association"),
        ]

    @property
    def is_active(self) -> bool:
        return self.status == AssociationMembershipStatus.ACTIVE

    def __str__(self):
        return f"{self.school} in {self.association} ({self.status})"


class AssociationAdministrator(models.Model):
    """Someone empowered to approve or suspend member schools for one
    association. Scoped to the association, not to any one school - a
    distinct link rather than a reuse of schools.Membership, since this
    person oversees a network of schools rather than working inside one."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    association = models.ForeignKey(SchoolProprietorAssociation, on_delete=models.CASCADE, related_name="administrators")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="association_administrator_roles")
    is_active = models.BooleanField(default=True)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["association", "user"], name="one_admin_link_per_user_per_association"),
        ]

    def __str__(self):
        return f"{self.user} administers {self.association}"


# --- The discovery layer -------------------------------------------------
#
# A NetworkStudentIdentity is the one thing that is ever allowed to cross the
# tenant boundary for a real child - never a copy of any school's Student
# row. Each school's own StudentNetworkEnrollment is the sole bridge back to
# its private Student; a school can never read another school's Student
# through this, only the fact that a shared identity exists and (within a
# shared association) whether it has a published TransferAlert.


class NetworkStudentIdentity(models.Model):
    """A single real child, as understood across the TransferVerify network.
    Created the first time a school enrolls a student into the network
    (today: the first time it publishes a bad-debt case for them) - never
    merged with another identity without a confirmed match, which needs more
    than a phone number (see later matching phases)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Network identity {self.id}"


class StudentNetworkEnrollment(models.Model):
    """The only bridge from a NetworkStudentIdentity to one school's own,
    private Student row - one row per (network identity, school), and at
    most one network identity per (school, student), so a school can never
    accidentally enroll the same local student twice under two identities."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    network_identity = models.ForeignKey(NetworkStudentIdentity, on_delete=models.CASCADE, related_name="enrollments")
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="network_enrollments")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="network_enrollments")

    #: E.164, the current number - a candidate lookup signal only, never
    #: sufficient alone to confirm identity (see apps.transferverify.network).
    guardian_phone_e164 = models.CharField(max_length=20, blank=True, db_index=True)
    #: Append-only [{"phone": "...", "changedAt": "..."}], oldest first. A
    #: changed number must never silently orphan matching history.
    guardian_phone_history = models.JSONField(default=list, blank=True)

    linked_at = models.DateTimeField(auto_now_add=True)
    linked_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="linked_network_enrollments")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["network_identity", "school"], name="enrollment_identity_school_uq"),
            models.UniqueConstraint(fields=["school", "student"], name="enrollment_student_school_uq"),
        ]
        indexes = [models.Index(fields=["guardian_phone_e164"], name="network_enrollment_phone_idx")]

    def __str__(self):
        return f"{self.school} enrollment for identity {self.network_identity_id}"


class TransferAlertState(models.TextChoices):
    ACTIVE = "active", "Active"
    VERIFICATION_PENDING = "verification_pending", "Verification pending"
    DISPUTED = "disputed", "Disputed"
    RESOLVED = "resolved", "Resolved"
    WITHDRAWN = "withdrawn", "Withdrawn"
    EXPIRED = "expired", "Expired"


class TransferAlert(models.Model):
    """The one thing another school can ever discover through TransferVerify
    - grown out of a Proprietor's own publish action on a BadDebtClassification,
    and only within the association(s) it was published to. Never embeds the
    source school's live ledger: amount/status/reason are a snapshot frozen
    at publication time, so a later correction to the source classification
    does not silently rewrite history other schools may already be relying
    on - see source_classification for the source school's own, private,
    always-current record."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    network_identity = models.ForeignKey(NetworkStudentIdentity, on_delete=models.PROTECT, related_name="transfer_alerts")
    source_school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="published_transfer_alerts")
    #: One classification ever grows at most one alert - a withdraw-then-
    #: republish reopens this same row rather than forking a new one, so the
    #: alert's own history stays a single auditable line.
    source_classification = models.OneToOneField(
        "BadDebtClassification", on_delete=models.CASCADE, related_name="transfer_alert"
    )
    #: Which associations this alert is currently discoverable within -
    #: copied from the classification's own association_scope at publish
    #: time, never guessed, never "every association".
    association_scope = models.JSONField(default=list, blank=True)
    state = models.CharField(max_length=24, choices=TransferAlertState.choices, default=TransferAlertState.ACTIVE)

    snapshot_status = models.CharField(max_length=24)
    snapshot_outstanding_amount_minor = models.PositiveIntegerField()
    snapshot_reason = models.CharField(max_length=32, blank=True)
    snapshot_note = models.TextField(blank=True)

    published_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="published_transfer_alerts")
    published_at = models.DateTimeField()
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-published_at"]
        indexes = [models.Index(fields=["network_identity", "state"], name="tv_alert_identity_state_idx")]

    def __str__(self):
        return f"TransferAlert {self.id} ({self.state})"


class NetworkSearchAudit(models.Model):
    """Every phone-candidate search is audited - who searched, when, and how
    many candidates came back. The raw phone number searched is deliberately
    not stored here: an audit log is itself a record several people may
    eventually see, and it does not need to become a second place a
    guardian's phone number is kept."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    searching_school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="network_searches")
    searching_membership = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="network_searches")
    result_count = models.PositiveIntegerField()
    searched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-searched_at"]


class TransferVerificationRequestStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    CONFIRMED = "confirmed", "Confirmed"
    REJECTED = "rejected", "Rejected"
    CANCELLED = "cancelled", "Cancelled"
    EXPIRED = "expired", "Expired"


class TransferVerificationRequest(models.Model):
    """One requesting school's factual question to the source school about a
    single TransferAlert it found through a candidate match - "does this
    still concern the student we are admitting, and what is its status?" A
    flat, single-transition state machine: the source school gives one
    factual response, never a back-and-forth negotiation inside the request
    itself - a dispute, if any, is its own separate object (a later phase)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transfer_alert = models.ForeignKey(TransferAlert, on_delete=models.CASCADE, related_name="verification_requests")
    requesting_school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="sent_verification_requests")
    requested_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="sent_verification_requests")
    status = models.CharField(
        max_length=16, choices=TransferVerificationRequestStatus.choices, default=TransferVerificationRequestStatus.PENDING
    )
    note = models.TextField(blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)

    responded_at = models.DateTimeField(null=True, blank=True)
    responded_by = models.ForeignKey(
        Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="answered_verification_requests"
    )
    response_note = models.TextField(blank=True)
    #: The classification's status at the moment of response, frozen - so a
    #: later change to the source school's own case never silently rewrites
    #: what the requesting school was actually told.
    response_status_snapshot = models.CharField(max_length=24, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            # One open request at a time per (alert, requesting school) - a
            # school cannot spam several pending requests for the same case.
            models.UniqueConstraint(
                fields=["transfer_alert", "requesting_school"],
                condition=Q(status=TransferVerificationRequestStatus.PENDING),
                name="one_pending_request_per_alert_school",
            ),
        ]

    def __str__(self):
        return f"Verification request {self.id} ({self.status})"


# --- The biometric layer -------------------------------------------------
#
# No fingerprint capture hardware or vendor SDK is integrated into SchoolOS
# today (see apps.transferverify.biometrics for the explicit design decision
# on this). These models are the real, tenant-aware data path a future
# capture integration will write to - consent, protected storage, revocation
# - built and tested now so the authority chain (see
# apps.transferverify.biometrics.evaluate_confidence) does not have to wait
# for that vendor decision.


class BiometricConsentStatus(models.TextChoices):
    GRANTED = "granted", "Granted"
    WITHDRAWN = "withdrawn", "Withdrawn"


class BiometricConsent(models.Model):
    """Explicit, revocable guardian consent to capture and store a
    biometric template for TransferVerify matching - required before any
    StudentBiometricTemplate may ever be captured for a student. Tenant-
    scoped: consent is given to, and recorded by, one school at a time."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="biometric_consents")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="biometric_consents")
    status = models.CharField(max_length=16, choices=BiometricConsentStatus.choices, default=BiometricConsentStatus.GRANTED)
    guardian_name = models.CharField(max_length=200)
    note = models.TextField(blank=True)
    recorded_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="recorded_biometric_consents")
    recorded_at = models.DateTimeField(auto_now_add=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawn_by = models.ForeignKey(
        Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="withdrawn_biometric_consents"
    )

    class Meta:
        ordering = ["-recorded_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "student"], condition=Q(status=BiometricConsentStatus.GRANTED),
                name="one_active_consent_per_student_school",
            ),
        ]

    @property
    def is_active(self) -> bool:
        return self.status == BiometricConsentStatus.GRANTED

    def __str__(self):
        return f"Consent for {self.student} at {self.school} ({self.status})"


class BiometricFinger(models.TextChoices):
    RIGHT_THUMB = "right_thumb", "Right thumb"
    RIGHT_INDEX = "right_index", "Right index"
    RIGHT_MIDDLE = "right_middle", "Right middle"
    RIGHT_RING = "right_ring", "Right ring"
    RIGHT_LITTLE = "right_little", "Right little"
    LEFT_THUMB = "left_thumb", "Left thumb"
    LEFT_INDEX = "left_index", "Left index"
    LEFT_MIDDLE = "left_middle", "Left middle"
    LEFT_RING = "left_ring", "Left ring"
    LEFT_LITTLE = "left_little", "Left little"
    OTHER = "other", "Other"


class StudentBiometricTemplate(models.Model):
    """A protected biometric template - never the raw scan/image, and never
    transferred between schools directly (only ever compared inside the
    matching service - see apps.transferverify.biometrics). Platform-level
    and linked to the NetworkStudentIdentity, not to any one school's
    Student row, because matching must work across schools. Not hardcoded to
    two fingers - a normal FK-many table, so a school may register as many
    as its capture device supports."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    network_identity = models.ForeignKey(NetworkStudentIdentity, on_delete=models.CASCADE, related_name="biometric_templates")
    enrolled_school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="enrolled_biometric_templates")
    consent = models.ForeignKey(BiometricConsent, on_delete=models.PROTECT, related_name="templates")
    finger = models.CharField(max_length=16, choices=BiometricFinger.choices)
    #: Opaque and encrypted at rest - never returned to any caller as-is,
    #: only ever read by the matching service itself.
    template_data = models.BinaryField()
    template_version = models.CharField(max_length=40)
    quality_score = models.PositiveSmallIntegerField(null=True, blank=True)
    source_device = models.CharField(max_length=120, blank=True)
    captured_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="captured_biometric_templates")
    captured_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="revoked_biometric_templates"
    )

    class Meta:
        ordering = ["-captured_at"]
        indexes = [models.Index(fields=["network_identity"], name="biometric_tpl_identity_idx")]

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def __str__(self):
        return f"{self.get_finger_display()} template for identity {self.network_identity_id}"
