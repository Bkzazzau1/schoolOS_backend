import uuid

from django.conf import settings
from django.db import models


class SchoolType(models.TextChoices):
    NURSERY = "nursery", "Nursery"
    PRIMARY = "primary", "Primary"
    SECONDARY = "secondary", "Secondary"
    NURSERY_PRIMARY = "nursery_primary", "Nursery + Primary"
    PRIMARY_SECONDARY = "primary_secondary", "Primary + Secondary"
    NURSERY_PRIMARY_SECONDARY = (
        "nursery_primary_secondary",
        "Nursery + Primary + Secondary",
    )
    COLLEGE = "college", "College"
    OTHER = "other", "Other"


class School(models.Model):
    """A tenant. Every record the app syncs belongs to exactly one school, and
    nothing is ever readable or writable across schools.

    ``organization`` is the commercial/account owner above the tenant. It is
    nullable so schools created before the SaaS account layer remain valid and
    can be attached deliberately later without changing their tenant identity.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="schools",
    )
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    school_type = models.CharField(max_length=32, choices=SchoolType.choices, blank=True)
    location = models.CharField(max_length=250, blank=True)
    is_active = models.BooleanField(default=True)
    #: The address emails to this school's people are sent from (for example
    #: office@brightgate.school.ng), and the name shown beside it. Set up for each
    #: school; if empty, the platform's default sender is used.
    official_email = models.EmailField(blank=True)
    official_sender_name = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["organization", "is_active"])]

    def __str__(self):
        return self.name


class Role(models.TextChoices):
    """Matches the app's SchoolRole enum, so names travel unchanged."""

    PROPRIETOR = "proprietor"
    ADMINISTRATOR = "administrator"
    PRINCIPAL = "principal"
    TEACHER = "teacher"
    ACCOUNTANT = "accountant"
    PARENT = "parent"
    STUDENT = "student"
    ALUMNI = "alumni"
    STAFF = "staff"
    DRIVER = "driver"


class Membership(models.Model):
    """One person's role at one school. A person can hold several (for example
    a teacher who is also a parent), each with its own id, which is what the
    app sends as membershipId."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "school", "role"], name="unique_role_per_user_per_school"
            )
        ]

    def __str__(self):
        return f"{self.user} · {self.role} · {self.school}"
