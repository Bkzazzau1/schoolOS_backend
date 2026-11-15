"""Platform-operator announcements: a marketing/informational "pop message" shown to
schools in the app, written and targeted by SkulGen staff through apps.console - never
generated automatically, and never anything a school's own staff can create for
themselves. Distinct from apps.notifications.Notification, which is a per-school,
per-membership inbox message, not a platform-wide broadcast."""

import uuid

from django.conf import settings
from django.db import models


class PlatformAnnouncement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=120)
    body = models.TextField()
    # Null means every school on the platform; set means just this organization's
    # schools - never scoped tighter than an organization, since this is a commercial/
    # platform-level message, not something a single school warrants on its own.
    organization = models.ForeignKey(
        "organizations.Organization",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="announcements",
    )
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-effective_from", "-id"]
        indexes = [
            models.Index(
                fields=["organization", "effective_from", "effective_until"],
                name="announcement_org_window_idx",
            )
        ]

    def __str__(self):
        audience = self.organization.name if self.organization_id else "all schools"
        return f"{self.title} · {audience}"
