import uuid

from django.db import models

from apps.academics.models import CurriculumTopic
from apps.schools.models import Membership, School
from apps.students.models import Student
from apps.timetable.models import TimetableEntry


class LessonAttendanceState(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted"


class AttendanceMark(models.TextChoices):
    UNMARKED = "unmarked", "Unmarked"
    PRESENT = "present", "Present"
    ABSENT = "absent", "Absent"
    LATE = "late", "Late"
    EXCUSED = "excused", "Excused"


class LessonAttendanceRegister(models.Model):
    """One subject-attendance register for one real timetable occurrence."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="lesson_attendance_registers",
    )
    external_id = models.CharField(max_length=128)
    timetable_entry = models.ForeignKey(
        TimetableEntry,
        on_delete=models.PROTECT,
        related_name="attendance_registers",
    )
    lesson_date = models.DateField()
    teacher_membership = models.ForeignKey(
        Membership,
        on_delete=models.PROTECT,
        related_name="lesson_attendance_registers",
    )
    curriculum_topic = models.ForeignKey(
        CurriculumTopic,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="attendance_registers",
    )
    state = models.CharField(
        max_length=16,
        choices=LessonAttendanceState.choices,
        default=LessonAttendanceState.DRAFT,
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="submitted_lesson_attendance_registers",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-lesson_date", "timetable_entry__starts_at", "external_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "external_id"],
                name="lesson_attendance_external_uq",
            ),
            models.UniqueConstraint(
                fields=["timetable_entry", "lesson_date"],
                name="lesson_attendance_occurrence_uq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["school", "lesson_date", "state"],
                name="lesson_att_school_date_idx",
            ),
            models.Index(
                fields=["teacher_membership", "lesson_date"],
                name="lesson_att_teacher_date_idx",
            ),
        ]

    def __str__(self):
        return f"{self.lesson_date} · {self.timetable_entry}"


class LessonAttendanceEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    register = models.ForeignKey(
        LessonAttendanceRegister,
        on_delete=models.CASCADE,
        related_name="entries",
    )
    student = models.ForeignKey(
        Student,
        on_delete=models.PROTECT,
        related_name="lesson_attendance_entries",
    )
    student_code = models.CharField(max_length=80)
    student_name = models.CharField(max_length=320)
    status = models.CharField(
        max_length=16,
        choices=AttendanceMark.choices,
        default=AttendanceMark.UNMARKED,
    )
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["student_name", "student_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["register", "student"],
                name="lesson_attendance_student_uq",
            )
        ]
        indexes = [
            models.Index(
                fields=["student", "status"],
                name="lesson_att_student_status_idx",
            )
        ]

    def __str__(self):
        return f"{self.register} · {self.student_code} · {self.status}"
