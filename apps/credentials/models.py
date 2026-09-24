import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.schools.models import Membership, School


class RecoveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RESOLVED = "resolved", "Resolved"
    DISMISSED = "dismissed", "Dismissed"


class CredentialRecoveryRequest(models.Model):
    """A non-enumerating password-recovery request routed to a school office.

    Student and Parent accounts may not have a verified email or SMS recovery
    channel. A public request therefore never changes credentials itself. It
    creates one school-scoped request that an authorized proprietor/admin can
    resolve through the credential-management API.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="credential_recovery_requests",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="credential_recovery_requests",
    )
    identity_kind = models.CharField(max_length=32)
    requested_identifier = models.CharField(max_length=160)
    status = models.CharField(
        max_length=16,
        choices=RecoveryStatus.choices,
        default=RecoveryStatus.PENDING,
    )
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_credential_recovery_requests",
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "user"],
                condition=Q(status="pending"),
                name="one_pending_credential_recovery_per_school_user",
            )
        ]
        indexes = [
            models.Index(
                fields=["school", "status", "created_at"],
                name="credentials_recovery_school_status_idx",
            )
        ]

    def __str__(self):
        return f"{self.school} · {self.user} · {self.status}"


class CredentialAuditEvent(models.Model):
    """Append-only evidence for credential-sensitive actions."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="credential_audit_events",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="credential_audit_events",
    )
    actor = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="credential_audit_events",
    )
    event = models.CharField(max_length=48)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["school", "user", "created_at"],
                name="credentials_audit_school_user_idx",
            )
        ]

    def __str__(self):
        return f"{self.event} · {self.user}"
