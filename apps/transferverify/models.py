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
