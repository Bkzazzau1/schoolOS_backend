import uuid
from decimal import Decimal

from django.db import models

from apps.academics.models import AcademicTerm, ClassSubject
from apps.schools.models import Membership, School
from apps.students.models import Student


class AssessmentType(models.TextChoices):
    CA = "ca", "Continuous Assessment"
    QUIZ = "quiz", "Quiz"
    TEST = "test", "Test"
    EXAM = "exam", "Exam"
    PRACTICAL = "practical", "Practical"
    PROJECT = "project", "Project"


class AssessmentState(models.TextChoices):
    DRAFT = "draft", "Draft"
    PUBLISHED = "published", "Published"
    SUBMITTED = "submitted", "Submitted for review"
    LOCKED = "locked", "Locked"
    RELEASED = "released", "Released"


class AssessmentDefinition(models.Model):
    """Canonical Teacher-authored assessment for one class-subject and term.

    Draft content is Teacher-editable. Publication snapshots the eligible
    learner roster and freezes type/maximum score/weight, matching how
    Assignment publication works. Score entry then happens against the
    frozen roster until the Teacher submits for review. Locking and release
    are a separate, explicit school action (Administrator/Proprietor), never
    something a Teacher device can do to its own submission.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="academic_assessments"
    )
    external_id = models.CharField(max_length=128)
    class_subject = models.ForeignKey(
        ClassSubject, on_delete=models.PROTECT, related_name="assessments"
    )
    term = models.ForeignKey(
        AcademicTerm, on_delete=models.PROTECT, related_name="assessments"
    )
    author_membership = models.ForeignKey(
        Membership,
        on_delete=models.PROTECT,
        related_name="authored_assessments",
    )
    last_edited_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="edited_assessments",
    )
    submitted_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="submitted_assessments",
    )
    locked_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="locked_assessments",
    )
    released_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="released_assessments",
    )
    assessment_type = models.CharField(
        max_length=16, choices=AssessmentType.choices, default=AssessmentType.CA
    )
    state = models.CharField(
        max_length=16, choices=AssessmentState.choices, default=AssessmentState.DRAFT
    )
    title = models.CharField(max_length=240, blank=True)
    maximum_score = models.PositiveIntegerField(default=0)
    # Relative weight of this assessment in the class-subject/term gradebook
    # average. A plain, school-wide default of 1.00 (equal weighting) unless
    # the Teacher sets otherwise; frozen at publication like maximum_score.
    weight = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("1.00"))
    version = models.PositiveIntegerField(default=1)
    submitted_at = models.DateTimeField(null=True, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "external_id"], name="assess_external_uq"
            )
        ]
        indexes = [
            models.Index(
                fields=["school", "state"], name="assess_school_state_idx"
            ),
            models.Index(
                fields=["class_subject", "term", "state"],
                name="assess_subject_term_idx",
            ),
        ]


class AssessmentRecipient(models.Model):
    """Learner roster frozen when an assessment is first published."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assessment = models.ForeignKey(
        AssessmentDefinition, on_delete=models.PROTECT, related_name="recipients"
    )
    student = models.ForeignKey(
        Student, on_delete=models.PROTECT, related_name="assessment_recipients"
    )
    student_code = models.CharField(max_length=80)
    student_name = models.CharField(max_length=320)
    admission_number = models.CharField(max_length=80, blank=True)
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["student_name", "student_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["assessment", "student"], name="assess_recipient_uq"
            )
        ]
        indexes = [
            models.Index(fields=["student", "assessment"], name="assess_student_idx")
        ]


class AssessmentScore(models.Model):
    """Current canonical mark for one recipient of one assessment.

    ``score`` is null while not yet entered - this is how the server tells
    "not entered" apart from an honest zero, which the earlier local-only
    prototype could not do.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assessment = models.ForeignKey(
        AssessmentDefinition, on_delete=models.PROTECT, related_name="scores"
    )
    recipient = models.OneToOneField(
        AssessmentRecipient, on_delete=models.PROTECT, related_name="score"
    )
    student = models.ForeignKey(
        Student, on_delete=models.PROTECT, related_name="assessment_scores"
    )
    score = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    entered_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    entered_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["student__surname", "student__first_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["assessment", "student"], name="assess_score_student_uq"
            )
        ]
        indexes = [
            models.Index(fields=["student", "assessment"], name="assess_score_idx")
        ]


class AssessmentEvent(models.Model):
    """Append-only lifecycle and correction history for one assessment.

    Every state transition (publish, submit, lock, release, return) and every
    score correction made after normal entry is closed off (submitted, locked
    or released) is recorded here. Normal score entry while PUBLISHED is not
    logged per keystroke - only the definition-level transitions and explicit
    corrections are, which is where auditability actually matters.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assessment = models.ForeignKey(
        AssessmentDefinition, on_delete=models.PROTECT, related_name="events"
    )
    revision = models.PositiveIntegerField()
    action = models.CharField(max_length=24)
    actor_membership = models.ForeignKey(
        Membership, on_delete=models.PROTECT, related_name="assessment_events"
    )
    comment = models.TextField(blank=True)
    # Populated only for a 'corrected' action.
    student = models.ForeignKey(
        Student,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    previous_score = models.DecimalField(
        max_digits=9, decimal_places=2, null=True, blank=True
    )
    new_score = models.DecimalField(
        max_digits=9, decimal_places=2, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["assessment", "revision"], name="assess_event_revision_uq"
            )
        ]
