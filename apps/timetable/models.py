import uuid

from django.db import models

from apps.academics.models import AcademicTerm, ClassSubject
from apps.schools.models import Membership, School


class TimetableDay(models.IntegerChoices):
    MONDAY = 1, "Monday"
    TUESDAY = 2, "Tuesday"
    WEDNESDAY = 3, "Wednesday"
    THURSDAY = 4, "Thursday"
    FRIDAY = 5, "Friday"
    SATURDAY = 6, "Saturday"
    SUNDAY = 7, "Sunday"


class TimetableEntry(models.Model):
    """One recurring weekly lesson slot for one canonical class-subject."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="timetable_entries",
    )
    external_id = models.CharField(max_length=64)
    term = models.ForeignKey(
        AcademicTerm,
        on_delete=models.PROTECT,
        related_name="timetable_entries",
    )
    class_subject = models.ForeignKey(
        ClassSubject,
        on_delete=models.PROTECT,
        related_name="timetable_entries",
    )
    day_of_week = models.PositiveSmallIntegerField(choices=TimetableDay.choices)
    period_number = models.PositiveSmallIntegerField()
    starts_at = models.TimeField()
    ends_at = models.TimeField()
    room = models.CharField(max_length=120, blank=True)
    is_active = models.BooleanField(default=True)
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
        ordering = ["term__starts_on", "day_of_week", "starts_at", "period_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "external_id"],
                name="tt_entry_external_uq",
            ),
            models.UniqueConstraint(
                fields=["term", "class_subject", "day_of_week", "period_number"],
                name="tt_entry_subject_period_uq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["term", "day_of_week", "starts_at"],
                name="tt_term_day_time_idx",
            ),
            models.Index(
                fields=["class_subject", "is_active"],
                name="tt_class_subject_idx",
            ),
        ]

    def __str__(self):
        return (
            f"{self.term.name} · {self.get_day_of_week_display()} · "
            f"{self.class_subject.academic_class.name} · {self.class_subject.subject.name}"
        )


class TimetableOverride(models.Model):
    """One date-specific substitution, room change or cancellation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="timetable_overrides",
    )
    external_id = models.CharField(max_length=64)
    timetable_entry = models.ForeignKey(
        TimetableEntry,
        on_delete=models.PROTECT,
        related_name="overrides",
    )
    lesson_date = models.DateField()
    substitute_teacher_membership = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="timetable_substitutions",
    )
    room = models.CharField(max_length=120, blank=True)
    note = models.TextField(blank=True)
    is_cancelled = models.BooleanField(default=False)
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
        ordering = ["lesson_date", "timetable_entry__starts_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "external_id"],
                name="tt_override_external_uq",
            ),
            models.UniqueConstraint(
                fields=["timetable_entry", "lesson_date"],
                name="tt_override_entry_date_uq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["lesson_date", "substitute_teacher_membership"],
                name="tt_override_date_teacher_idx",
            )
        ]

    def __str__(self):
        return f"{self.lesson_date} · {self.timetable_entry}"
