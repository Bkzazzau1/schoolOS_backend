import uuid

from django.conf import settings
from django.db import models


class Organization(models.Model):
    """The commercial account above one or more SchoolOS school tenants.

    An organization may represent an individual proprietor, a school group,
    company, NGO or another entity that owns/manages schools. School operational
    data remains tenant-scoped to ``schools.School``; this model never replaces
    the school as the tenant boundary.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_organizations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class OrganizationRole(models.TextChoices):
    OWNER = "owner", "Account owner"
    ADMINISTRATOR = "administrator", "Account administrator"
    BILLING_ADMINISTRATOR = "billing_administrator", "Billing administrator"


class OrganizationMembership(models.Model):
    """A person's account-level authority across an organization.

    This is intentionally separate from ``schools.Membership``. Being an
    organization owner/admin decides account-level actions such as provisioning
    schools; it does not itself grant operational access inside a school.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_memberships",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    role = models.CharField(max_length=32, choices=OrganizationRole.choices)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization", "role"],
                name="unique_org_role_per_user",
            )
        ]
        indexes = [
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["organization", "is_active"]),
        ]

    @property
    def can_create_schools(self) -> bool:
        return self.role in {
            OrganizationRole.OWNER,
            OrganizationRole.ADMINISTRATOR,
        }

    def __str__(self):
        return f"{self.user} · {self.role} · {self.organization}"


class OrganizationAuditEvent(models.Model):
    """Append-only history of sensitive organization-level changes."""

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="audit_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    action = models.CharField(max_length=64)
    target_type = models.CharField(max_length=32, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    detail = models.JSONField(default=dict)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["organization", "-at"])]

    def __str__(self):
        return f"{self.organization} · {self.action} · {self.at}"
