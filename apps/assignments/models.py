import uuid

from django.db import models

from apps.academics.models import AcademicTerm, ClassSubject, CurriculumTopic
from apps.schools.models import Membership, School
from apps.students.models import Student


class AssignmentType(models.TextChoices):
    HOMEWORK = "homework", "Homework"
    CLASSWORK = "classwork", "Classwork"
    PROJECT = "project", "Project"
    REVISION = "revision", "Revision"


class AssignmentState(models.TextChoices):
    DRAFT = "draft", "Draft"
    PUBLISHED = "published", "Published"
    CLOSED = "closed", "Closed"


class SubmissionState(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted"
    RETURNED = "returned", "Returned for revision"
    GRADED = "graded", "Graded"


class Assignment(models.Model):
    """Canonical Teacher-authored work for one class-subject and term.

    Draft content is Teacher-editable. Publication snapshots the learner roster
    and creates an append-only publication revision. Published class/subject,
    type, topic and maximum score are immutable; explicit revisions may update
    only learner-facing wording and due time.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="academic_assignments"
    )
    external_id = models.CharField(max_length=128)
    class_subject = models.ForeignKey(
        ClassSubject, on_delete=models.PROTECT, related_name="assignments"
    )
    term = models.ForeignKey(
        AcademicTerm, on_delete=models.PROTECT, related_name="assignments"
    )
    curriculum_topic = models.ForeignKey(
        CurriculumTopic,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="assignments",
    )
    author_membership = models.ForeignKey(
        Membership,
        on_delete=models.PROTECT,
        related_name="authored_assignments",
    )
    last_edited_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="edited_assignments",
    )
    published_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="published_assignments",
    )
    closed_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="closed_assignments",
    )
    assignment_type = models.CharField(
        max_length=16, choices=AssignmentType.choices, default=AssignmentType.HOMEWORK
    )
    state = models.CharField(
        max_length=16, choices=AssignmentState.choices, default=AssignmentState.DRAFT
    )
    title = models.CharField(max_length=240, blank=True)
    instructions = models.TextField(blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    maximum_score = models.PositiveIntegerField(default=0)
    version = models.PositiveIntegerField(default=1)
    published_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "external_id"], name="assign_external_uq"
            )
        ]
        indexes = [
            models.Index(
                fields=["school", "state", "due_at"], name="assign_school_state_idx"
            ),
            models.Index(
                fields=["class_subject", "term", "state"],
                name="assign_subject_term_idx",
            ),
        ]


class AssignmentPublication(models.Model):
    """Append-only exact publication/revision snapshot."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assignment = models.ForeignKey(
        Assignment, on_delete=models.PROTECT, related_name="publications"
    )
    revision = models.PositiveIntegerField()
    snapshot = models.JSONField(default=dict)
    published_by = models.ForeignKey(
        Membership, on_delete=models.PROTECT, related_name="assignment_publications"
    )
    published_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        # The snapshot is an audit record of what learners received at this
        # exact revision. Stamp the revision onto the stored snapshot itself so
        # revision 1 cannot accidentally retain the pre-publication value 0.
        snapshot = dict(self.snapshot or {})
        snapshot["publicationRevision"] = self.revision
        self.snapshot = snapshot
        super().save(*args, **kwargs)

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["assignment", "revision"], name="assign_pub_revision_uq"
            )
        ]


class AssignmentRecipient(models.Model):
    """Learner roster frozen when an assignment is first published."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assignment = models.ForeignKey(
        Assignment, on_delete=models.PROTECT, related_name="recipients"
    )
    student = models.ForeignKey(
        Student, on_delete=models.PROTECT, related_name="assignment_recipients"
    )
    student_code = models.CharField(max_length=80)
    student_name = models.CharField(max_length=320)
    admission_number = models.CharField(max_length=80, blank=True)
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["student_name", "student_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["assignment", "student"], name="assign_recipient_uq"
            )
        ]
        indexes = [
            models.Index(fields=["student", "assignment"], name="assign_student_idx")
        ]


class AssignmentSubmission(models.Model):
    """Current canonical state of one learner's response to one assignment."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="assignment_submissions"
    )
    external_id = models.CharField(max_length=128)
    assignment = models.ForeignKey(
        Assignment, on_delete=models.PROTECT, related_name="submissions"
    )
    recipient = models.OneToOneField(
        AssignmentRecipient,
        on_delete=models.PROTECT,
        related_name="submission",
    )
    student = models.ForeignKey(
        Student, on_delete=models.PROTECT, related_name="assignment_submissions"
    )
    state = models.CharField(
        max_length=16, choices=SubmissionState.choices, default=SubmissionState.DRAFT
    )
    response_text = models.TextField(blank=True)
    attempt_number = models.PositiveIntegerField(default=0)
    submitted_at = models.DateTimeField(null=True, blank=True)
    is_late = models.BooleanField(default=False)
    score = models.DecimalField(
        max_digits=9, decimal_places=2, null=True, blank=True
    )
    feedback = models.TextField(blank=True)
    graded_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="graded_assignment_submissions",
    )
    graded_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-submitted_at", "student__surname", "student__first_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "external_id"], name="assign_sub_external_uq"
            ),
            models.UniqueConstraint(
                fields=["assignment", "student"], name="assign_sub_student_uq"
            ),
        ]
        indexes = [
            models.Index(
                fields=["assignment", "state", "submitted_at"],
                name="assign_sub_state_idx",
            ),
            models.Index(fields=["student", "state"], name="assign_sub_student_idx"),
        ]


class AssignmentSubmissionVersion(models.Model):
    """Append-only response snapshot for every submit/resubmit."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(
        AssignmentSubmission, on_delete=models.PROTECT, related_name="versions"
    )
    attempt_number = models.PositiveIntegerField()
    assignment_revision = models.PositiveIntegerField(default=1)
    snapshot = models.JSONField(default=dict)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-attempt_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "attempt_number"], name="assign_sub_attempt_uq"
            )
        ]


class AssignmentGradeEvent(models.Model):
    """Append-only Teacher marking/return history."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(
        AssignmentSubmission, on_delete=models.PROTECT, related_name="grade_events"
    )
    revision = models.PositiveIntegerField()
    action = models.CharField(max_length=16)
    score = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    feedback = models.TextField(blank=True)
    actor_membership = models.ForeignKey(
        Membership, on_delete=models.PROTECT, related_name="assignment_grade_events"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "revision"], name="assign_grade_revision_uq"
            )
        ]
