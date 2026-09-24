import uuid

from django.db import models

from apps.schools.models import Membership, School
from apps.students.models import Student


class SubjectRequirement(models.TextChoices):
    COMPULSORY = "compulsory", "Compulsory"
    ELECTIVE = "elective", "Elective"


class Subject(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="academic_subjects"
    )
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "code"], name="unique_subject_code_per_school"
            ),
            models.UniqueConstraint(
                fields=["school", "name"], name="unique_subject_name_per_school"
            ),
        ]

    def __str__(self):
        return f"{self.school} · {self.name}"


class ClassSubject(models.Model):
    """A subject offered by one canonical class in one academic session."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        "academics.AcademicSession",
        on_delete=models.PROTECT,
        related_name="class_subjects",
    )
    academic_class = models.ForeignKey(
        "academics.AcademicClass",
        on_delete=models.PROTECT,
        related_name="session_subjects",
    )
    subject = models.ForeignKey(
        Subject, on_delete=models.PROTECT, related_name="class_offerings"
    )
    requirement = models.CharField(
        max_length=16,
        choices=SubjectRequirement.choices,
        default=SubjectRequirement.COMPULSORY,
    )
    periods_per_week = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["academic_class__level_order", "subject__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "academic_class", "subject"],
                name="unique_subject_per_class_session",
            )
        ]
        indexes = [
            models.Index(
                fields=["session", "academic_class", "is_active"],
                name="acad_cls_subject_active_idx",
            )
        ]

    def __str__(self):
        return f"{self.session.name} · {self.academic_class.name} · {self.subject.name}"


class StudentSubjectSelection(models.Model):
    """Only individual elective choices are stored; compulsory subjects are derived."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    student = models.ForeignKey(
        Student, on_delete=models.PROTECT, related_name="subject_selections"
    )
    class_subject = models.ForeignKey(
        ClassSubject, on_delete=models.PROTECT, related_name="student_selections"
    )
    selected = models.BooleanField(default=True)
    selected_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["student__surname", "student__first_name", "class_subject__subject__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "class_subject"],
                name="unique_student_subject_selection",
            )
        ]


class TeachingAssignment(models.Model):
    """One teaching responsibility for a class-subject in a session."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    class_subject = models.OneToOneField(
        ClassSubject,
        on_delete=models.PROTECT,
        related_name="teaching_assignment",
    )
    # Stable staff-directory id. It exists before a staff login is linked.
    teacher_staff_id = models.CharField(max_length=128)
    # Filled only when the staff record is linked to an active Teacher membership.
    teacher_membership = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="canonical_teaching_assignments",
    )
    assigned_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["class_subject__academic_class__level_order", "class_subject__subject__name"]
        indexes = [
            models.Index(
                fields=["teacher_membership", "is_active"],
                name="acad_teacher_assign_idx",
            )
        ]


class TeachingAssignmentEvent(models.Model):
    """Append-only reassignment history."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assignment = models.ForeignKey(
        TeachingAssignment, on_delete=models.PROTECT, related_name="events"
    )
    from_teacher_staff_id = models.CharField(max_length=128, blank=True)
    to_teacher_staff_id = models.CharField(max_length=128)
    reason = models.TextField(blank=True)
    actor = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at", "-id"]
