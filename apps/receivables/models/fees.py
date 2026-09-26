import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.academics.models import AcademicClass, AcademicSession, AcademicTerm
from apps.schools.models import Membership, School
from apps.students.models import Student

from ..constants import DEFAULT_CURRENCY
from .family import Family


class ScheduleStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PUBLISHED = "published", "Published"
    RETIRED = "retired", "Retired"


class FeeCategory(models.TextChoices):
    TUITION = "tuition", "Tuition"
    ICT = "ict", "ICT"
    LEVY = "levy", "Development levy"
    BOOKS = "books", "Books"
    TRANSPORT = "transport", "Transport"
    MEALS = "meals", "Meals"
    EXAMINATION = "examination", "Examination"
    UNIFORM = "uniform", "Uniform"
    OTHER = "other", "Other"


class FeeScope(models.TextChoices):
    ALL = "all", "Every student"
    SECTION = "section", "An academic section"
    CLASS = "class", "One class"
    STUDENT = "student", "One student"


class FeeSchedule(models.Model):
    """A school's fee structure for a session (and, usually, a term): what is charged, to whom, and
    when it falls due. It is a DRAFT until someone with billing authority publishes it; publishing is
    what turns it into charges, and a published schedule is never edited - it is corrected by explicit
    adjustments, voids or a new schedule, so the record of what was charged stays true."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="fee_schedules")
    session = models.ForeignKey(AcademicSession, on_delete=models.PROTECT, related_name="fee_schedules")
    term = models.ForeignKey(AcademicTerm, null=True, blank=True, on_delete=models.PROTECT, related_name="fee_schedules")
    name = models.CharField(max_length=120)
    status = models.CharField(max_length=12, choices=ScheduleStatus.choices, default=ScheduleStatus.DRAFT)
    #: The schedule this one was made from, when it corrects or follows another.
    replaces = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="replacements")
    created_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    published_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    published_at = models.DateTimeField(null=True, blank=True)
    retired_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    retired_at = models.DateTimeField(null=True, blank=True)
    retire_reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "session", "term", "name"],
                condition=~Q(status="retired"),
                name="unique_live_fee_schedule_name",
            ),
            # A schedule that was published has a time; one that was retired without ever being published
            # (made in error) has none.
            models.CheckConstraint(
                condition=Q(status__in=["draft", "retired"]) | Q(published_at__isnull=False),
                name="fee_schedule_published_has_a_time",
            ),
        ]
        indexes = [models.Index(fields=["school", "status"])]

    def save(self, *args, **kwargs):
        if self.session.school_id != self.school_id:
            raise ValidationError("A fee schedule and its session must belong to the same school.")
        if self.term_id and self.term.session_id != self.session_id:
            raise ValidationError("The term must belong to the schedule's session.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.status})"


class FeeItem(models.Model):
    """One line of a schedule: a named charge, its amount, who it applies to and when it falls due.
    Money is whole kobo. Items can be changed only while their schedule is a draft."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    schedule = models.ForeignKey(FeeSchedule, on_delete=models.PROTECT, related_name="items")
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=16, choices=FeeCategory.choices, default=FeeCategory.OTHER)
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    #: An optional item (transport, meals) is charged only to students it is explicitly aimed at.
    is_mandatory = models.BooleanField(default=True)
    #: When it falls due, unless `plan` splits it into instalments.
    due_date = models.DateField(null=True, blank=True)
    #: Instalments: a list of `{"basisPoints": 5000, "dueDate": "2026-09-30"}` whose basis points add up to
    #: 10000. Empty means one payment, on `due_date`.
    plan = models.JSONField(default=list, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    scope = models.CharField(max_length=8, choices=FeeScope.choices, default=FeeScope.ALL)
    section = models.CharField(max_length=80, blank=True)
    academic_class = models.ForeignKey(AcademicClass, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    student = models.ForeignKey(Student, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["schedule", "code"], name="unique_fee_item_code_per_schedule"),
            models.CheckConstraint(condition=Q(amount_minor__gt=0), name="fee_item_amount_positive"),
            models.CheckConstraint(
                condition=(
                    Q(scope="all", section="", academic_class__isnull=True, student__isnull=True)
                    | Q(scope="section", academic_class__isnull=True, student__isnull=True) & ~Q(section="")
                    | Q(scope="class", section="", academic_class__isnull=False, student__isnull=True)
                    | Q(scope="student", section="", academic_class__isnull=True, student__isnull=False)
                ),
                name="fee_item_scope_matches_its_target",
            ),
            models.CheckConstraint(condition=Q(scope="student") | Q(is_mandatory=True), name="optional_fee_item_targets_students"),
        ]

    def _schedule_is_draft(self) -> bool:
        # What is stored, not a cached copy: a stale object must not be able to slip an edit past the freeze.
        return FeeSchedule.objects.filter(pk=self.schedule_id, status=ScheduleStatus.DRAFT).exists()

    def save(self, *args, **kwargs):
        if not self._schedule_is_draft():
            raise ValidationError("A published fee schedule is never edited: correct it with adjustments or a new schedule.")
        self.school_id = self.schedule.school_id
        if self.academic_class_id and self.academic_class.school_id != self.school_id:
            raise ValidationError("The class must belong to the schedule's school.")
        if self.student_id and self.student.school_id != self.school_id:
            raise ValidationError("The student must belong to the schedule's school.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if not self._schedule_is_draft():
            raise ValidationError("A published fee schedule is never edited.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.name} {self.amount_minor}"


class ReceivableStatus(models.TextChoices):
    OPEN = "open", "Open"
    PARTIALLY_PAID = "partially_paid", "Partly paid"
    SETTLED = "settled", "Settled"
    VOID = "void", "Void"


#: What can never change on a receivable once it exists: the charge itself. (Its status, being derived
#: from the ledger, and the void fields can change; who it belongs to changes only through the service.)
_FROZEN = ("school_id", "student_id", "schedule_id", "fee_item_id", "gross_amount_minor", "due_date", "currency", "charge_key", "installment_number")


class StudentReceivable(models.Model):
    """One charge a student owes: a fee item, or one instalment of it. The GROSS amount and due date
    are fixed at publication and never rewritten. Discounts, scholarships and waivers are separate
    `ReceivableAdjustment` rows, and payments are allocations and credit applications, so what was
    charged, what was taken off and what was paid can each be traced.

    `status` is a cache of what the ledger says (see `ledger.derive_status`); nothing else about the
    money is stored on this row, so it cannot drift from the ledger.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="receivables")
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="receivables")
    family = models.ForeignKey(Family, on_delete=models.PROTECT, related_name="receivables")
    schedule = models.ForeignKey(FeeSchedule, on_delete=models.PROTECT, related_name="receivables")
    fee_item = models.ForeignKey(FeeItem, on_delete=models.PROTECT, related_name="receivables")
    session = models.ForeignKey(AcademicSession, on_delete=models.PROTECT, related_name="+")
    term = models.ForeignKey(AcademicTerm, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    #: What the item was called when it was charged, kept so history reads correctly if it is later renamed.
    item_code = models.CharField(max_length=40)
    item_name = models.CharField(max_length=120)
    item_category = models.CharField(max_length=16, choices=FeeCategory.choices)
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    gross_amount_minor = models.BigIntegerField()
    due_date = models.DateField()
    status = models.CharField(max_length=16, choices=ReceivableStatus.choices, default=ReceivableStatus.OPEN)
    #: Instalments of one charge share a `charge_key`; a discount on the charge is spread across them.
    charge_key = models.UUIDField(default=uuid.uuid4)
    installment_number = models.PositiveSmallIntegerField(default=1)
    installment_count = models.PositiveSmallIntegerField(default=1)
    published_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    voided_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["due_date", "created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["fee_item", "student", "installment_number"], name="one_receivable_per_item_student_instalment"),
            models.CheckConstraint(condition=Q(gross_amount_minor__gt=0), name="receivable_gross_positive"),
            models.CheckConstraint(
                condition=(Q(status="void", voided_at__isnull=False) | (~Q(status="void") & Q(voided_at__isnull=True))),
                name="receivable_void_matches_status",
            ),
            models.CheckConstraint(condition=Q(installment_number__gte=1, installment_number__lte=models.F("installment_count")), name="receivable_instalment_in_range"),
        ]
        indexes = [
            models.Index(fields=["school", "family", "status"]),
            models.Index(fields=["school", "student", "status"]),
            models.Index(fields=["school", "due_date"]),
            models.Index(fields=["charge_key"]),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            if not (self.student.school_id == self.family.school_id == self.schedule.school_id == self.school_id):
                raise ValidationError("A receivable, its student, its family and its schedule must all belong to one school.")
        else:
            stored = StudentReceivable.objects.filter(pk=self.pk).values(*_FROZEN).first()
            if stored is not None and any(stored[name] != getattr(self, name) for name in _FROZEN):
                raise ValidationError("A published charge is never rewritten: use an adjustment, a void or a new charge.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A charge is never deleted: void it, with a reason.")

    def __str__(self):
        return f"{self.item_name} {self.gross_amount_minor} ({self.status})"


class AdjustmentKind(models.TextChoices):
    SCHOLARSHIP = "scholarship", "Scholarship"
    DISCOUNT = "discount", "Discount"
    WAIVER = "waiver", "Waiver"
    CORRECTION = "correction", "Correction"
    OTHER = "other", "Other"


class ReceivableAdjustment(models.Model):
    """An amount taken off what one charge requires. Always positive and always a reduction: a
    scholarship of ₦30,000 on a ₦125,000 fee is a row of 3000000, and the net payable is what is left.
    Rows are never edited or deleted. To undo one, a REVERSAL row (`reverses`) of the same amount is
    added, so what was decided, and that it was later undone, both stay on record.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    receivable = models.ForeignKey(StudentReceivable, on_delete=models.PROTECT, related_name="adjustments")
    kind = models.CharField(max_length=16, choices=AdjustmentKind.choices)
    amount_minor = models.BigIntegerField()
    reason = models.CharField(max_length=300)
    reverses = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversal")
    #: Adjustments made together across the instalments of one charge share a group.
    group_id = models.UUIDField(null=True, blank=True)
    requested_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    authorized_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    authorized_at = models.DateTimeField(default=timezone.now)
    #: Where it came from when it came from something else, like `concession:CNC-2026-041`. Applying the
    #: same source twice makes one adjustment, never two.
    source_ref = models.CharField(max_length=120, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount_minor__gt=0), name="adjustment_amount_positive"),
            models.UniqueConstraint(fields=["receivable", "source_ref"], condition=~Q(source_ref=""), name="one_adjustment_per_source_per_receivable"),
        ]
        indexes = [models.Index(fields=["school", "receivable"])]

    @property
    def is_reversal(self) -> bool:
        return self.reverses_id is not None

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("An adjustment is never edited: reverse it and make a new one.")
        if self.receivable.school_id != self.school_id:
            raise ValidationError("An adjustment and its receivable must belong to the same school.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("An adjustment is never deleted: reverse it.")
