import uuid

from django.db import models

from apps.academics.models import AcademicTerm, ClassSubject
from apps.schools.models import Membership, School
from apps.students.models import Student


class CbtTestState(models.TextChoices):
    DRAFT = "draft", "Draft"
    PUBLISHED = "published", "Published"
    CLOSED = "closed", "Closed"


class CbtResultMode(models.TextChoices):
    SCORE_ONLY = "score_only", "Score only"
    SCORE_AND_ANSWERS = "score_and_answers", "Score and correct answers"


class CbtTest(models.Model):
    """A Teacher-authored computer-based practice test for one class subject
    and term. Publication freezes the eligible-student roster (see
    apps.lesson_attendance.eligible_students_for_class_subject) and every
    question's content - a Student never receives a correct answer until
    their own attempt is submitted, and only then if result_mode allows it
    (see apps.cbt.visibility). This is a low-stakes practice tool, not a
    formal graded exam: unlike Assessments, publishing makes it immediately
    available to students - there is no separate Administrator release step.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="cbt_tests")
    external_id = models.CharField(max_length=128)
    class_subject = models.ForeignKey(
        ClassSubject, on_delete=models.PROTECT, related_name="cbt_tests"
    )
    term = models.ForeignKey(AcademicTerm, on_delete=models.PROTECT, related_name="cbt_tests")
    author_membership = models.ForeignKey(
        Membership, on_delete=models.PROTECT, related_name="authored_cbt_tests"
    )
    last_edited_by = models.ForeignKey(
        Membership, null=True, blank=True, on_delete=models.PROTECT, related_name="edited_cbt_tests"
    )
    published_by = models.ForeignKey(
        Membership, null=True, blank=True, on_delete=models.PROTECT, related_name="published_cbt_tests"
    )
    closed_by = models.ForeignKey(
        Membership, null=True, blank=True, on_delete=models.PROTECT, related_name="closed_cbt_tests"
    )
    state = models.CharField(max_length=16, choices=CbtTestState.choices, default=CbtTestState.DRAFT)
    title = models.CharField(max_length=240, blank=True)
    duration_minutes = models.PositiveIntegerField(default=15)
    instructions = models.TextField(blank=True)
    result_mode = models.CharField(
        max_length=20, choices=CbtResultMode.choices, default=CbtResultMode.SCORE_ONLY
    )
    version = models.PositiveIntegerField(default=1)
    published_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["school", "external_id"], name="cbt_test_external_uq")
        ]
        indexes = [
            models.Index(fields=["school", "state"], name="cbt_test_school_state_idx"),
            models.Index(
                fields=["class_subject", "term", "state"], name="cbt_test_subject_term_idx"
            ),
        ]


class CbtQuestion(models.Model):
    """One question, embedded in the test's own sync payload (a Teacher
    authors many questions together as one draft, not one at a time as
    separate mutations). correct_index and explanation are server-only data
    never sent to a Student before their own attempt is submitted."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    test = models.ForeignKey(CbtTest, on_delete=models.CASCADE, related_name="questions")
    sequence = models.PositiveSmallIntegerField()
    prompt = models.TextField()
    options = models.JSONField(default=list)
    correct_index = models.PositiveSmallIntegerField()
    explanation = models.TextField(blank=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(fields=["test", "sequence"], name="cbt_question_seq_uq")
        ]


class CbtRecipient(models.Model):
    """Learner roster frozen when a test is first published. Server-side
    bookkeeping only - never synced as its own entity (a Student's own
    CbtAttempt record is what the client reads)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    test = models.ForeignKey(CbtTest, on_delete=models.PROTECT, related_name="recipients")
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="cbt_recipients")
    student_code = models.CharField(max_length=80)
    student_name = models.CharField(max_length=320)
    admission_number = models.CharField(max_length=80, blank=True)
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["student_name", "student_code"]
        constraints = [
            models.UniqueConstraint(fields=["test", "student"], name="cbt_recipient_uq")
        ]
        indexes = [models.Index(fields=["student", "test"], name="cbt_recipient_student_idx")]


class CbtAttempt(models.Model):
    """One recipient's own attempt - started, answered question by question,
    then submitted. score is null until submitted and is always computed
    server-side from CbtQuestion.correct_index, never trusted from the
    client's own claimed answers."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="cbt_attempts")
    external_id = models.CharField(max_length=128)
    test = models.ForeignKey(CbtTest, on_delete=models.PROTECT, related_name="attempts")
    recipient = models.OneToOneField(CbtRecipient, on_delete=models.PROTECT, related_name="attempt")
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="cbt_attempts")
    started_at = models.DateTimeField(null=True, blank=True)
    deadline_at = models.DateTimeField(null=True, blank=True)
    answers = models.JSONField(default=list)
    submitted_at = models.DateTimeField(null=True, blank=True)
    score = models.PositiveIntegerField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-submitted_at", "student__surname", "student__first_name"]
        constraints = [
            models.UniqueConstraint(fields=["school", "external_id"], name="cbt_attempt_external_uq"),
            models.UniqueConstraint(fields=["test", "student"], name="cbt_attempt_student_uq"),
        ]
        indexes = [
            models.Index(fields=["test", "student"], name="cbt_attempt_test_student_idx"),
        ]

    @property
    def submitted(self):
        return self.submitted_at is not None


class CbtEvent(models.Model):
    """Append-only lifecycle history: created, published, closed."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    test = models.ForeignKey(CbtTest, on_delete=models.PROTECT, related_name="events")
    revision = models.PositiveIntegerField()
    action = models.CharField(max_length=24)
    actor_membership = models.ForeignKey(
        Membership, on_delete=models.PROTECT, related_name="cbt_events"
    )
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(fields=["test", "revision"], name="cbt_event_revision_uq")
        ]
