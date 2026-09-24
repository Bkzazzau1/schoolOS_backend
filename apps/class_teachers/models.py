import uuid

from django.db import models
from django.db.models import Q

from apps.academics.models import AcademicClass, AcademicSession
from apps.schools.models import Membership, School


class ClassTeacherAssignment(models.Model):
    """Effective-dated responsibility for being the class/form teacher of one
    whole AcademicClass for one AcademicSession - the same handover-safe shape
    as apps.academics.TeachingAssignment, but scoped to a class as a whole
    rather than one class-subject, and for a session rather than a term (a
    class teacher conventionally stays for the whole academic year).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="class_teacher_assignments"
    )
    external_id = models.CharField(max_length=64)
    academic_class = models.ForeignKey(
        AcademicClass, on_delete=models.PROTECT, related_name="class_teacher_assignments"
    )
    session = models.ForeignKey(
        AcademicSession, on_delete=models.PROTECT, related_name="class_teacher_assignments"
    )
    teacher_membership = models.ForeignKey(
        Membership,
        on_delete=models.PROTECT,
        related_name="class_teacher_assignments",
    )
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    assigned_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    handover_reason = models.TextField(blank=True)
    previous_assignment = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="handover_successors",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "external_id"], name="class_teacher_external_uq"
            ),
            models.UniqueConstraint(
                fields=["academic_class", "session"],
                condition=Q(ended_at__isnull=True),
                name="clsteach_active_class_uq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["teacher_membership", "ended_at"],
                name="clsteach_member_active_idx",
            )
        ]

    @property
    def is_active(self):
        return self.ended_at is None

    def __str__(self):
        return f"{self.academic_class} · {self.session} · {self.teacher_membership}"
