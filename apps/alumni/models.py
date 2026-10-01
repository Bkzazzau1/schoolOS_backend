import uuid

from django.core.exceptions import ValidationError
from django.db import models

from apps.schools.models import Membership, Role, School


class AlumniVerificationStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    VERIFIED = "verified", "Verified"
    REJECTED = "rejected", "Rejected"


class AlumniProfile(models.Model):
    """School-scoped identity for a former student.

    The historical student record remains separate. This profile represents the
    person's current alumni identity and can coexist with another membership
    such as parent, staff or teacher.
    """

    membership = models.OneToOneField(
        Membership,
        on_delete=models.CASCADE,
        related_name="alumni_profile",
        primary_key=True,
    )
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="alumni_profiles",
    )
    original_student_reference = models.CharField(max_length=120, blank=True)
    admission_number = models.CharField(max_length=80, blank=True)
    graduation_year = models.PositiveSmallIntegerField(null=True, blank=True)
    graduation_set = models.CharField(max_length=120, blank=True)
    verification_status = models.CharField(
        max_length=20,
        choices=AlumniVerificationStatus.choices,
        default=AlumniVerificationStatus.PENDING,
    )
    profession = models.CharField(max_length=160, blank=True)
    organisation = models.CharField(max_length=200, blank=True)
    location_text = models.CharField(max_length=160, blank=True)
    bio = models.TextField(blank=True)
    directory_visible = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    verification_note = models.CharField(max_length=500, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        Membership,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_alumni_profiles",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-graduation_year", "membership_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "admission_number"],
                condition=~models.Q(admission_number=""),
                name="unique_alumni_admission_number_per_school",
            )
        ]

    def clean(self):
        if self.membership_id:
            if self.membership.role != Role.ALUMNI:
                raise ValidationError("Alumni profiles require an alumni membership.")
            if self.school_id and self.membership.school_id != self.school_id:
                raise ValidationError("The alumni profile and membership must belong to the same school.")
        if self.verified_by_id and self.school_id:
            if self.verified_by.school_id != self.school_id:
                raise ValidationError("The alumni verifier must belong to the same school.")
        if self.directory_visible and self.verification_status != AlumniVerificationStatus.VERIFIED:
            raise ValidationError("Only verified alumni can appear in the alumni directory.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.membership.user} · alumni · {self.school}"


class AlumniVerificationEvent(models.Model):
    """Append-only history of Alumni identity review decisions and resubmissions."""

    class Event(models.TextChoices):
        TRANSITIONED = "transitioned", "Transitioned"
        SUBMITTED = "submitted", "Submitted"
        RESUBMITTED = "resubmitted", "Resubmitted"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"

    profile = models.ForeignKey(
        AlumniProfile,
        on_delete=models.CASCADE,
        related_name="verification_events",
    )
    actor = models.ForeignKey(
        Membership,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="alumni_verification_events",
    )
    event = models.CharField(max_length=20, choices=Event.choices)
    note = models.CharField(max_length=500, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["at", "id"]

    def clean(self):
        if self.actor_id and self.actor.school_id != self.profile.school_id:
            raise ValidationError("The Alumni verification actor must belong to the same school.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.profile_id} · {self.event} · {self.at}"


class AlumniEvent(models.Model):
    """A real reunion/event the school has created for its own Alumni - school management only
    creates these; an alumnus browses and RSVPs (see AlumniEventRsvp), never proposes their own."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="alumni_events")
    title = models.CharField(max_length=200)
    date = models.DateField()
    time_text = models.CharField(max_length=40, blank=True)
    venue = models.CharField(max_length=200, blank=True)
    note = models.TextField(blank=True)
    created_by = models.ForeignKey(
        Membership,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_alumni_events",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]

    def clean(self):
        if self.created_by_id and self.created_by.school_id != self.school_id:
            raise ValidationError("The event creator must belong to the same school.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} · {self.date} · {self.school}"


class AlumniEventRsvp(models.Model):
    """One real, updatable row per (event, alumnus) - unlike a read receipt, an RSVP is a current
    answer a person may genuinely change, not a historical log of every change."""

    event = models.ForeignKey(AlumniEvent, on_delete=models.CASCADE, related_name="rsvps")
    membership = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="alumni_event_rsvps")
    attending = models.BooleanField()
    responded_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "membership"], name="one_rsvp_per_alumnus_per_event")
        ]

    def clean(self):
        if self.membership_id and self.membership.role != Role.ALUMNI:
            raise ValidationError("Only an Alumni membership may RSVP.")
        if self.membership_id and self.event_id and self.membership.school_id != self.event.school_id:
            raise ValidationError("The RSVP and the event must belong to the same school.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.event_id} · {self.membership_id} · {'attending' if self.attending else 'not attending'}"


class AlumniPledgeCategory(models.TextChoices):
    VOLUNTEERING = "volunteering", "Volunteering"
    MENTORING = "mentoring", "Mentoring"
    SUPPLIES = "supplies", "Supplies & Materials"
    SPEAKING = "speaking", "Guest Speaking"
    OTHER = "other", "Other"


class AlumniPledgeStatus(models.TextChoices):
    OFFERED = "offered", "Offered"
    ACKNOWLEDGED = "acknowledged", "Acknowledged"
    FULFILLED = "fulfilled", "Fulfilled"
    WITHDRAWN = "withdrawn", "Withdrawn"


class AlumniPledge(models.Model):
    """A real, non-monetary offer of help from a real Alumni membership - volunteering,
    mentoring, supplies, guest speaking. One-sided: an alumnus puts this forward, school
    management reviews it. Never real money; that stays a separate, unbuilt concern."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="alumni_pledges")
    membership = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="alumni_pledges")
    category = models.CharField(max_length=20, choices=AlumniPledgeCategory.choices)
    description = models.TextField()
    status = models.CharField(
        max_length=20, choices=AlumniPledgeStatus.choices, default=AlumniPledgeStatus.OFFERED
    )
    school_note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        if self.membership_id and self.membership.role != Role.ALUMNI:
            raise ValidationError("Only an Alumni membership may make a pledge.")
        if self.membership_id and self.membership.school_id != self.school_id:
            raise ValidationError("The pledge and the alumnus must belong to the same school.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.membership_id} · {self.category} · {self.status}"


class AlumniOpportunityType(models.TextChoices):
    FULL_TIME = "full_time", "Full-time"
    PART_TIME = "part_time", "Part-time"
    INTERNSHIP = "internship", "Internship"
    CONTRACT = "contract", "Contract"
    OTHER = "other", "Other"


class AlumniOpportunityStatus(models.TextChoices):
    OPEN = "open", "Open"
    CLOSED = "closed", "Closed"


class AlumniOpportunity(models.Model):
    """A real job/opportunity posting from a real alumnus to fellow alumni - a posting board, not
    a curated school listing: any real alumnus may post, and its own poster (or school management,
    for moderation) may close it. Interested alumni follow up directly through `contact_info`; this
    is not an application-tracking system."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="alumni_opportunities")
    posted_by = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="alumni_opportunities")
    title = models.CharField(max_length=200)
    organisation = models.CharField(max_length=200)
    opportunity_type = models.CharField(max_length=20, choices=AlumniOpportunityType.choices)
    location_text = models.CharField(max_length=160, blank=True)
    description = models.TextField()
    contact_info = models.CharField(max_length=200, blank=True)
    status = models.CharField(
        max_length=10, choices=AlumniOpportunityStatus.choices, default=AlumniOpportunityStatus.OPEN
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        if self.posted_by_id and self.posted_by.role != Role.ALUMNI:
            raise ValidationError("Only an Alumni membership may post an opportunity.")
        if self.posted_by_id and self.posted_by.school_id != self.school_id:
            raise ValidationError("The opportunity and its poster must belong to the same school.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} · {self.organisation} · {self.status}"
