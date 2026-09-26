import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.schools.models import Membership, School

from ..constants import (
    MANDATE_PROVIDER_CODES,
    ConnectionStatus,
    Environment,
    WebhookStatus,
)


class MandateProviderConnection(models.Model):
    """The SCHOOL'S OWN relationship with a direct-debit provider (Remita or Lendsqr), as SchoolOS holds it.

    The school onboards with the provider directly, receives its own credentials and enters them here. SchoolOS uses them to make mandates
    for payers, to send approved debit instructions and to read the provider's answers; the provider executes the debit and moves the money.

    A school may connect BOTH providers at once. There is no "active provider": each mandate records the connection it was made under for
    good, so different families can use different providers. This is a table of its own, not Smart Money Collection's.

    Everything here is safe to show a signed-in finance user. The credential itself exists only as `sealed_credentials` (see vault.py) and is
    never serialised anywhere.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="mandate_connections")
    provider = models.CharField(max_length=40)
    environment = models.CharField(max_length=8, choices=Environment.choices, default=Environment.LIVE)

    #: What the provider reports about the school's merchant profile, once verified. Never a secret.
    merchant_name = models.CharField(max_length=200, blank=True)
    merchant_reference = models.CharField(max_length=60, blank=True)
    #: Non-secret choices the school made for this provider.
    provider_settings = models.JSONField(default=dict, blank=True)
    label = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=16, choices=ConnectionStatus.choices, default=ConnectionStatus.PENDING)

    sealed_credentials = models.BinaryField(blank=True, default=b"")
    #: Only ever a hash, for looking the connection up from the address the provider calls.
    webhook_token_hash = models.CharField(max_length=64, blank=True, db_index=True)
    webhook_status = models.CharField(max_length=16, choices=WebhookStatus.choices, default=WebhookStatus.NOT_CONFIGURED)
    webhook_confirmed_at = models.DateTimeField(null=True, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    #: A short code, never a provider's own message: those can echo what was sent.
    last_error_code = models.CharField(max_length=40, blank=True)
    provider_meta = models.JSONField(default=dict, blank=True)
    is_sandbox = models.BooleanField(default=False)

    created_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    disconnected_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            # One live connection per provider per school (a replaced credential is the same connection).
            models.UniqueConstraint(fields=["school", "provider"], condition=~Q(status=ConnectionStatus.REVOKED), name="one_live_mandate_connection_per_provider"),
            models.CheckConstraint(condition=Q(provider__in=MANDATE_PROVIDER_CODES), name="mandate_connection_is_a_mandate_provider"),
        ]
        indexes = [models.Index(fields=["school", "status"])]

    def __str__(self):
        return f"{self.provider} {self.environment} ({self.school_id})"


class MandateAuditEvent(models.Model):
    """Who did what in Mandates & Direct Debit, and when. Written for every material change and never edited or deleted. Details are scrubbed
    of anything that looks like a secret, and never carry a bank account number."""

    #: A running number, so events within the same instant still read in the order they happened.
    id = models.BigAutoField(primary_key=True)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    actor = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=40)
    object_type = models.CharField(max_length=40)
    object_id = models.CharField(max_length=64)
    detail = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["school", "-at"]), models.Index(fields=["school", "object_type", "object_id"])]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("A mandate audit event is never edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A mandate audit event is never deleted.")


class MandateProviderEvent(models.Model):
    """Every provider callback that reached us, by payload hash: the same delivery twice is recognised and not processed twice."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=40)
    connection = models.ForeignKey(MandateProviderConnection, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    payload_hash = models.CharField(max_length=64)
    authenticity_valid = models.BooleanField(default=False)
    event_type = models.CharField(max_length=24, blank=True)
    outcome = models.CharField(max_length=24, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["provider", "payload_hash"], name="unique_mandate_provider_event")]
        ordering = ["-received_at"]
