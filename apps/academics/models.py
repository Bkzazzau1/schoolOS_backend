import uuid

from django.db import models
from django.db.models import Q

from apps.schools.models import Membership, School
from apps.students.models import Student, StudentEnrollment


class AcademicLifecycleStatus(models.TextChoices):
    PLANNED = "planned", "Planned"
    ACTIVE = "active", "Active"
    CLOSED = "closed", "Closed"


class AcademicSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="academic_sessions"
    )
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=120)
    starts_on = models.DateField()
    ends_on = models.DateField()
    status = models.CharField(
        max_length=16,
        choices=AcademicLifecycleStatus.choices,
        default=AcademicLifecycleStatus.PLANNED,
    )
    created_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-starts_on", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "code"],
                name="unique_academic_session_code_per_school",
            ),
            models.UniqueConstraint(
                fields=["school"],
                condition=Q(status="active"),
                name="one_active_academic_session_per_school",
            ),
        ]

    def __str__(self):
        return f"{self.school} · {self.name}"


class AcademicTerm(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        AcademicSession, on_delete=models.CASCADE, related_name="terms"
    )
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=80)
    sequence = models.PositiveSmallIntegerField()
    starts_on = models.DateField()
    ends_on = models.DateField()
    status = models.CharField(
        max_length=16,
        choices=AcademicLifecycleStatus.choices,
        default=AcademicLifecycleStatus.PLANNED,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["session__starts_on", "sequence", "starts_on"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "code"],
                name="unique_academic_term_code_per_session",
            ),
            models.UniqueConstraint(
                fields=["session", "sequence"],
                name="unique_academic_term_sequence_per_session",
            ),
            models.UniqueConstraint(
                fields=["session"],
                condition=Q(status="active"),
                name="one_active_academic_term_per_session",
            ),
        ]

    @property
    def school(self):
        return self.session.school

    def __str__(self):
        return f"{self.session.name} · {self.name}"


class AcademicClass(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="academic_classes"
    )
    code = models.CharField(max_length=60)
    name = models.CharField(max_length=120)
    section = models.CharField(max_length=80)
    level_order = models.PositiveSmallIntegerField()
    stream = models.CharField(max_length=60, blank=True)
    next_class = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="previous_classes",
    )
    is_terminal = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["level_order", "name", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "code"],
                name="unique_academic_class_code_per_school",
            ),
            models.UniqueConstraint(
                fields=["school", "name"],
                name="unique_academic_class_name_per_school",
            ),
        ]
        indexes = [
            models.Index(
                fields=["school", "is_active", "level_order"],
                name="academics_class_order_idx",
            )
        ]

    def __str__(self):
        return f"{self.school} · {self.name}"


class EnrollmentAcademicContext(models.Model):
    """Immutable academic-year/class context attached to one enrollment row."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    enrollment = models.OneToOneField(
        StudentEnrollment,
        on_delete=models.CASCADE,
        related_name="academic_context",
    )
    session = models.ForeignKey(
        AcademicSession,
        on_delete=models.PROTECT,
        related_name="enrollment_contexts",
    )
    academic_class = models.ForeignKey(
        AcademicClass,
        on_delete=models.PROTECT,
        related_name="enrollment_contexts",
    )
    entry_term = models.ForeignKey(
        AcademicTerm,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="entry_enrollment_contexts",
    )
    source = models.CharField(max_length=40, default="automatic")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class ProgressionBatchStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    REVIEW = "review", "Ready for review"
    APPLIED = "applied", "Applied"
    CANCELLED = "cancelled", "Cancelled"


class ProgressionOutcome(models.TextChoices):
    PROMOTE = "promote", "Promote"
    REPEAT = "repeat", "Repeat"
    TRANSFER_OUT = "transfer_out", "Transfer out"
    GRADUATE = "graduate", "Graduate"
    HOLD = "hold", "Hold"


class ProgressionBatch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="progression_batches"
    )
    external_id = models.CharField(max_length=128)
    from_session = models.ForeignKey(
        AcademicSession,
        on_delete=models.PROTECT,
        related_name="outgoing_progression_batches",
    )
    to_session = models.ForeignKey(
        AcademicSession,
        on_delete=models.PROTECT,
        related_name="incoming_progression_batches",
    )
    source_class = models.ForeignKey(
        AcademicClass,
        on_delete=models.PROTECT,
        related_name="progression_batches",
    )
    status = models.CharField(
        max_length=16,
        choices=ProgressionBatchStatus.choices,
        default=ProgressionBatchStatus.DRAFT,
    )
    approved_by = models.CharField(max_length=200, blank=True)
    note = models.TextField(blank=True)
    created_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "external_id"],
                name="unique_progression_batch_external_id_per_school",
            )
        ]


class ProgressionDecision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(
        ProgressionBatch, on_delete=models.CASCADE, related_name="decisions"
    )
    student = models.ForeignKey(
        Student, on_delete=models.PROTECT, related_name="progression_decisions"
    )
    outcome = models.CharField(max_length=20, choices=ProgressionOutcome.choices)
    target_class = models.ForeignKey(
        AcademicClass,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="incoming_progression_decisions",
    )
    records_pack_ready = models.BooleanField(default=False)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["student__surname", "student__first_name", "student__student_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "student"],
                name="unique_progression_decision_per_batch_student",
            )
        ]


# Keep these models in a separate source file so the academic calendar/progression
# definitions above stay readable, while importing them here follows Django's
# normal models-module discovery path.
from .curriculum_models import (  # noqa: E402,F401
    ClassSubject,
    StudentSubjectSelection,
    Subject,
    SubjectRequirement,
    TeachingAssignment,
    TeachingAssignmentEvent,
)
