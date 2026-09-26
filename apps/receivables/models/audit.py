import uuid

from django.core.exceptions import ValidationError
from django.db import models

from apps.schools.models import Membership, School


class FinanceAuditEvent(models.Model):
    """Who did what to a school's financial records, and when. Written for every sensitive change and
    never edited or deleted. Details are scrubbed of anything that looks like a secret."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    #: Empty when the system acted on its own (for example matching a payment).
    actor = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=48)
    object_type = models.CharField(max_length=40)
    object_id = models.CharField(max_length=64)
    detail = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [
            models.Index(fields=["school", "-at"]),
            models.Index(fields=["school", "object_type", "object_id"]),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("A finance audit event is never edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A finance audit event is never deleted.")
