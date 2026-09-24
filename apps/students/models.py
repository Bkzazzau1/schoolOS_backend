import uuid

from django.db import models
from django.db.models import Q

from apps.schools.models import Membership, School


class AdmissionStage(models.TextChoices):
    NEW = "newApplication", "New"
    DOCUMENTS = "documents", "Documents"
    SCREENING = "screening", "Screening"
    OFFER = "offer", "Offer"
    ACCEPTED = "accepted", "Accepted"
    REGISTERED = "registered", "Registered"


class AdmissionDocumentStatus(models.TextChoices):
    RECEIVED = "received", "Received"
    PENDING = "pending", "Pending"


class AdmissionApplication(models.Model):
    """An applicant is not a student until registration activation succeeds."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="admission_applications"
    )
    reference = models.CharField(max_length=128)
    applicant_name = models.CharField(max_length=240)
    section = models.CharField(max_length=80)
    proposed_class = models.CharField(max_length=120)
    guardian_name = models.CharField(max_length=200)
    guardian_phone = models.CharField(max_length=40)
    stage = models.CharField(
        max_length=24, choices=AdmissionStage.choices, default=AdmissionStage.NEW
    )
    source = models.CharField(max_length=80, default="School website")
    submitted_label = models.CharField(max_length=40, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    birth_certificate = models.CharField(
        max_length=16,
        choices=AdmissionDocumentStatus.choices,
        default=AdmissionDocumentStatus.PENDING,
    )
    previous_school_report = models.CharField(
        max_length=16,
        choices=AdmissionDocumentStatus.choices,
        default=AdmissionDocumentStatus.PENDING,
    )
    guardian_identification = models.CharField(
        max_length=16,
        choices=AdmissionDocumentStatus.choices,
        default=AdmissionDocumentStatus.PENDING,
    )
    document_request_queued = models.BooleanField(default=False)
    closed_reason = models.CharField(max_length=250, blank=True)
    created_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-submitted_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "reference"],
                name="unique_admission_reference_per_school",
            )
        ]
        indexes = [
            models.Index(fields=["school", "stage"], name="students_adm_stage_idx")
        ]

    def __str__(self):
        return f"{self.reference} · {self.applicant_name}"


class RegistrationStatus(models.TextChoices):
    IN_PROGRESS = "in_progress", "Admission in progress"
    ACTIVE = "active", "Active"


class StudentStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    TRANSFER_PENDING = "transfer_pending", "Transfer pending"
    TRANSFERRED_OUT = "transferred_out", "Transferred out"
    GRADUATED = "graduated", "Graduated"
    WITHDRAWN = "withdrawn", "Withdrawn"
    INACTIVE = "inactive", "Inactive"


class Student(models.Model):
    """Canonical tenant-scoped student identity.

    The UUID is safe for QR/barcode references. Admission number and student code
    are permanent school-facing identifiers and are never reused within a school.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="students")
    admission_number = models.CharField(max_length=80)
    student_code = models.CharField(max_length=80)
    first_name = models.CharField(max_length=120)
    surname = models.CharField(max_length=120)
    other_name = models.CharField(max_length=160, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=40, blank=True)
    previous_school = models.CharField(max_length=200, blank=True)
    address = models.TextField(blank=True)
    status = models.CharField(
        max_length=24, choices=StudentStatus.choices, default=StudentStatus.ACTIVE
    )
    activated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["surname", "first_name", "student_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "admission_number"],
                name="unique_student_admission_number_per_school",
            ),
            models.UniqueConstraint(
                fields=["school", "student_code"],
                name="unique_student_code_per_school",
            ),
        ]
        indexes = [
            models.Index(
                fields=["school", "status"], name="students_school_status_idx"
            )
        ]

    @property
    def full_name(self):
        return " ".join(
            value
            for value in [self.first_name, self.other_name, self.surname]
            if value
        )

    def __str__(self):
        return f"{self.student_code} · {self.full_name}"


class StudentRegistration(models.Model):
    """Registration workflow record before and after canonical activation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="student_registrations"
    )
    registration_id = models.CharField(max_length=128)
    source_applicant = models.ForeignKey(
        AdmissionApplication,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="registrations",
    )
    student = models.OneToOneField(
        Student,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="registration",
    )
    first_name = models.CharField(max_length=120)
    surname = models.CharField(max_length=120)
    other_name = models.CharField(max_length=160, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=40, blank=True)
    academic_section = models.CharField(max_length=80)
    proposed_class = models.CharField(max_length=120)
    previous_school = models.CharField(max_length=200, blank=True)
    address = models.TextField(blank=True)
    admission_number = models.CharField(max_length=80)
    student_code = models.CharField(max_length=80)
    status = models.CharField(
        max_length=16,
        choices=RegistrationStatus.choices,
        default=RegistrationStatus.IN_PROGRESS,
    )
    primary_guardian = models.CharField(max_length=200)
    relationship = models.CharField(max_length=60, blank=True)
    guardian_phone = models.CharField(max_length=40)
    guardian_email = models.EmailField(blank=True)
    family_account_ref = models.CharField(max_length=160, blank=True)
    sibling_link = models.CharField(max_length=160, blank=True)
    birth_certificate_status = models.CharField(max_length=120, blank=True)
    previous_school_record_status = models.CharField(max_length=120, blank=True)
    guardian_identification_status = models.CharField(max_length=120, blank=True)
    finance_setup_status = models.CharField(max_length=120, blank=True)
    transport_meal_status = models.CharField(max_length=120, blank=True)
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
        ordering = ["-updated_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "registration_id"],
                name="unique_registration_id_per_school",
            ),
            models.UniqueConstraint(
                fields=["school", "admission_number"],
                condition=~Q(admission_number=""),
                name="unique_registration_admission_per_school",
            ),
            models.UniqueConstraint(
                fields=["school", "student_code"],
                condition=~Q(student_code=""),
                name="unique_registration_student_code_per_school",
            ),
        ]

    def __str__(self):
        return f"{self.registration_id} · {self.first_name} {self.surname}"


class GuardianLink(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, related_name="guardians"
    )
    name = models.CharField(max_length=200)
    relationship = models.CharField(max_length=60, blank=True)
    phone = models.CharField(max_length=40)
    email = models.EmailField(blank=True)
    is_primary = models.BooleanField(default=False)
    family_account_ref = models.CharField(max_length=160, blank=True)
    sibling_link = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["student", "phone"],
                name="unique_guardian_phone_per_student",
            )
        ]


class EnrollmentStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"
    TRANSFERRED_OUT = "transferred_out", "Transferred out"
    GRADUATED = "graduated", "Graduated"
    WITHDRAWN = "withdrawn", "Withdrawn"


class StudentEnrollment(models.Model):
    """Append-only placement history; class movement creates a new row."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="student_enrollments"
    )
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, related_name="enrollments"
    )
    academic_section = models.CharField(max_length=80)
    class_name = models.CharField(max_length=120)
    status = models.CharField(
        max_length=24,
        choices=EnrollmentStatus.choices,
        default=EnrollmentStatus.ACTIVE,
    )
    is_billable = models.BooleanField(default=True)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    source_registration = models.ForeignKey(
        StudentRegistration,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="enrollments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["student"],
                condition=Q(status="active"),
                name="one_active_enrollment_per_student",
            )
        ]
        indexes = [
            models.Index(
                fields=["school", "status", "is_billable"],
                name="students_enrollment_bill_idx",
            )
        ]


class LifecycleStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


class StudentLifecycleEvent(models.Model):
    """Append-preserved operational movement and exit history."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="student_lifecycle_events"
    )
    external_id = models.CharField(max_length=128)
    student = models.ForeignKey(
        Student, on_delete=models.PROTECT, related_name="lifecycle_events"
    )
    workflow = models.CharField(max_length=40)
    change = models.CharField(max_length=250, blank=True)
    from_class = models.CharField(max_length=120, blank=True)
    to_class = models.CharField(max_length=120, blank=True)
    status = models.CharField(
        max_length=16,
        choices=LifecycleStatus.choices,
        default=LifecycleStatus.PENDING,
    )
    requested_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.CharField(max_length=200, blank=True)
    records_pack_ready = models.BooleanField(default=False)
    note = models.TextField(blank=True)
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
        ordering = ["-requested_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "external_id"],
                name="unique_lifecycle_external_id_per_school",
            )
        ]
        indexes = [
            models.Index(
                fields=["school", "status"], name="students_lifecycle_status_idx"
            )
        ]


class SchoolRosterRevision(models.Model):
    """Monotonic school roster revision used to version billing meters."""

    school = models.OneToOneField(
        School,
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="roster_revision",
    )
    revision = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.school} · roster {self.revision}"
