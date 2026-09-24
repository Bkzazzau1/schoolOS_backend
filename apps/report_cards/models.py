import uuid

from django.db import models

from apps.academics.models import AcademicClass, AcademicTerm, ClassSubject
from apps.schools.models import Membership, School
from apps.students.models import Student


class ReportCardState(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted for review"
    REVIEWED = "reviewed", "Reviewed"
    RELEASED = "released", "Released"


class ReportCard(models.Model):
    """One student's compiled term report.

    Subject lines and the overall average/grade are computed from released
    canonical Assessments (apps.assessments) - never re-entered by hand and
    never able to disagree with a released assessment score. Generating or
    regenerating replaces the lines and the computed fields; it does not
    replace an already-recorded Principal comment, submission or release,
    which stay put unless an explicit action changes them.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="report_cards"
    )
    external_id = models.CharField(max_length=128)
    student = models.ForeignKey(
        Student, on_delete=models.PROTECT, related_name="report_cards"
    )
    term = models.ForeignKey(
        AcademicTerm, on_delete=models.PROTECT, related_name="report_cards"
    )
    academic_class = models.ForeignKey(
        AcademicClass, on_delete=models.PROTECT, related_name="report_cards"
    )
    state = models.CharField(
        max_length=16, choices=ReportCardState.choices, default=ReportCardState.DRAFT
    )
    overall_average = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    overall_grade = models.CharField(max_length=4, blank=True)
    # Null until a class-wide position compilation has run, and again whenever
    # this student has no subject line with evidence to rank by.
    class_position = models.PositiveSmallIntegerField(null=True, blank=True)
    class_size = models.PositiveSmallIntegerField(null=True, blank=True)
    # From apps.lesson_attendance.services.student_term_attendance_percent:
    # this student's percent of SUBMITTED subject registers marked present
    # or late across the term's date range. Null when no submitted register
    # has ever marked them, never approximated to 0 or 100.
    attendance_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    principal_comment = models.TextField(blank=True)
    # Written by whoever holds the class's active ClassTeacherAssignment
    # (apps.class_teachers) at the time of writing - never fabricated when no
    # class teacher is assigned yet.
    class_teacher_comment = models.TextField(blank=True)
    generated_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="generated_report_cards",
    )
    submitted_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="submitted_report_cards",
    )
    reviewed_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reviewed_report_cards",
    )
    released_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="released_report_cards",
    )
    version = models.PositiveIntegerField(default=1)
    generated_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "external_id"], name="report_card_external_uq"
            ),
            models.UniqueConstraint(
                fields=["student", "term"], name="report_card_student_term_uq"
            ),
        ]
        indexes = [
            models.Index(
                fields=["academic_class", "term", "state"],
                name="report_card_class_term_idx",
            ),
        ]


class ReportCardSubjectLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report_card = models.ForeignKey(
        ReportCard, on_delete=models.CASCADE, related_name="lines"
    )
    class_subject = models.ForeignKey(
        ClassSubject, on_delete=models.PROTECT, related_name="report_card_lines"
    )
    weighted_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    grade = models.CharField(max_length=4, blank=True)
    assessments_included = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["class_subject__subject__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["report_card", "class_subject"], name="report_card_line_uq"
            )
        ]


class ReportCardEvent(models.Model):
    """Append-only lifecycle history: generated, submitted, reviewed,
    returned, released."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report_card = models.ForeignKey(
        ReportCard, on_delete=models.PROTECT, related_name="events"
    )
    revision = models.PositiveIntegerField()
    action = models.CharField(max_length=24)
    actor_membership = models.ForeignKey(
        Membership, on_delete=models.PROTECT, related_name="report_card_events"
    )
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["report_card", "revision"], name="report_card_event_revision_uq"
            )
        ]
