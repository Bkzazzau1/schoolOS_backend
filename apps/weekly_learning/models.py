import uuid

from django.db import models

from apps.academics.models import AcademicTerm, ClassSubject
from apps.schools.models import Membership, School


class WeeklyLearningState(models.TextChoices):
    DRAFT = "draft", "Draft"
    PUBLISHED = "published", "Published"


class WeeklyLearningUpdate(models.Model):
    """One Teacher-authored weekly family update for one ClassSubject.

    Factual learning evidence is derived from canonical LessonDeliveryRecord rows.
    The Teacher owns only the interpretive fields. Once published, the update is
    immutable and the exact family-facing payload is preserved in
    WeeklyLearningPublication.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="weekly_learning_updates"
    )
    external_id = models.CharField(max_length=128)
    class_subject = models.ForeignKey(
        ClassSubject,
        on_delete=models.PROTECT,
        related_name="weekly_learning_updates",
    )
    term = models.ForeignKey(
        AcademicTerm,
        on_delete=models.PROTECT,
        related_name="weekly_learning_updates",
    )
    week_start = models.DateField()
    week_end = models.DateField()
    author_membership = models.ForeignKey(
        Membership,
        on_delete=models.PROTECT,
        related_name="authored_weekly_learning_updates",
    )
    last_edited_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="edited_weekly_learning_updates",
    )
    published_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="published_weekly_learning_updates",
    )
    state = models.CharField(
        max_length=16,
        choices=WeeklyLearningState.choices,
        default=WeeklyLearningState.DRAFT,
    )
    next_focus = models.TextField(blank=True)
    support_note = models.TextField(blank=True)
    parent_note = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = [
            "-week_start",
            "class_subject__academic_class__level_order",
            "class_subject__subject__name",
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "external_id"],
                name="weekly_learning_external_uq",
            ),
            models.UniqueConstraint(
                fields=["class_subject", "term", "week_start"],
                name="wkly_learn_subject_week_uq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["school", "week_start", "state"],
                name="wkly_learn_school_week_idx",
            ),
            models.Index(
                fields=["class_subject", "week_start"],
                name="wkly_learn_subject_week_idx",
            ),
        ]


class WeeklyLearningPublication(models.Model):
    """Append-only exact snapshot of what became family-visible."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    update = models.ForeignKey(
        WeeklyLearningUpdate,
        on_delete=models.PROTECT,
        related_name="publications",
    )
    revision = models.PositiveIntegerField(default=1)
    snapshot = models.JSONField(default=dict)
    published_by = models.ForeignKey(
        Membership,
        on_delete=models.PROTECT,
        related_name="weekly_learning_publications",
    )
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-published_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["update", "revision"],
                name="wkly_learn_pub_revision_uq",
            )
        ]
