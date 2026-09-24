import uuid

from django.db import models

from apps.academics.models import ClassSubject, CurriculumTopic
from apps.lesson_attendance.models import LessonAttendanceRegister
from apps.schools.models import Membership, School
from apps.timetable.models import TimetableEntry


class LessonPlanState(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted"
    APPROVED = "approved", "Approved"
    NEEDS_CHANGES = "needs_changes", "Needs changes"


class LessonPlanReviewDecision(models.TextChoices):
    APPROVED = "approved", "Approved"
    NEEDS_CHANGES = "needs_changes", "Needs changes"


class LessonDeliveryState(models.TextChoices):
    DRAFT = "draft", "Draft"
    DELIVERED = "delivered", "Delivered"


class LessonPlan(models.Model):
    """Teacher preparation for exactly one timetable occurrence.

    The occurrence identity and curriculum topic are immutable. Content may be
    revised while draft/returned. Principal review changes state through an
    append-only LessonPlanReview rather than by trusting a Teacher payload.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="lesson_plans"
    )
    external_id = models.CharField(max_length=128)
    timetable_entry = models.ForeignKey(
        TimetableEntry, on_delete=models.PROTECT, related_name="lesson_plans"
    )
    lesson_date = models.DateField()
    class_subject = models.ForeignKey(
        ClassSubject, on_delete=models.PROTECT, related_name="lesson_plans"
    )
    curriculum_topic = models.ForeignKey(
        CurriculumTopic, on_delete=models.PROTECT, related_name="lesson_plans"
    )
    author_membership = models.ForeignKey(
        Membership,
        on_delete=models.PROTECT,
        related_name="authored_lesson_plans",
    )
    state = models.CharField(
        max_length=20,
        choices=LessonPlanState.choices,
        default=LessonPlanState.DRAFT,
    )
    objectives = models.TextField(blank=True)
    starter = models.TextField(blank=True)
    activities = models.TextField(blank=True)
    assessment = models.TextField(blank=True)
    resources = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reviewed_lesson_plans",
    )
    review_comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-lesson_date", "timetable_entry__starts_at", "external_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "external_id"],
                name="lesson_plan_external_uq",
            ),
            models.UniqueConstraint(
                fields=["timetable_entry", "lesson_date"],
                name="lesson_plan_occurrence_uq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["school", "lesson_date", "state"],
                name="lesson_plan_school_date_idx",
            ),
            models.Index(
                fields=["author_membership", "state"],
                name="lesson_plan_author_state_idx",
            ),
        ]


class LessonPlanReview(models.Model):
    """Append-only Principal decision against one submitted plan version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="lesson_plan_reviews"
    )
    external_id = models.CharField(max_length=128)
    plan = models.ForeignKey(
        LessonPlan, on_delete=models.PROTECT, related_name="reviews"
    )
    reviewer_membership = models.ForeignKey(
        Membership,
        on_delete=models.PROTECT,
        related_name="lesson_plan_review_events",
    )
    plan_version = models.PositiveIntegerField()
    decision = models.CharField(
        max_length=20,
        choices=LessonPlanReviewDecision.choices,
    )
    comment = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-reviewed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "external_id"],
                name="lesson_plan_review_external_uq",
            ),
            models.UniqueConstraint(
                fields=["plan", "plan_version"],
                name="lesson_plan_review_version_uq",
            ),
        ]


class LessonDeliveryRecord(models.Model):
    """Canonical evidence that one scheduled occurrence was actually taught."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="lesson_delivery_records"
    )
    external_id = models.CharField(max_length=128)
    timetable_entry = models.ForeignKey(
        TimetableEntry,
        on_delete=models.PROTECT,
        related_name="delivery_records",
    )
    lesson_date = models.DateField()
    plan = models.ForeignKey(
        LessonPlan, on_delete=models.PROTECT, related_name="delivery_records"
    )
    curriculum_topic = models.ForeignKey(
        CurriculumTopic,
        on_delete=models.PROTECT,
        related_name="delivery_records",
    )
    teacher_membership = models.ForeignKey(
        Membership,
        on_delete=models.PROTECT,
        related_name="lesson_delivery_records",
    )
    attendance_register = models.ForeignKey(
        LessonAttendanceRegister,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="lesson_delivery_records",
    )
    state = models.CharField(
        max_length=16,
        choices=LessonDeliveryState.choices,
        default=LessonDeliveryState.DRAFT,
    )
    reflection = models.TextField(blank=True)
    homework = models.TextField(blank=True)
    topic_completed = models.BooleanField(default=False)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-lesson_date", "timetable_entry__starts_at", "external_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "external_id"],
                name="lesson_delivery_external_uq",
            ),
            models.UniqueConstraint(
                fields=["timetable_entry", "lesson_date"],
                name="lesson_delivery_occurrence_uq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["school", "lesson_date", "state"],
                name="lesson_del_school_date_idx",
            ),
            models.Index(
                fields=["curriculum_topic", "state"],
                name="lesson_del_topic_state_idx",
            ),
        ]
