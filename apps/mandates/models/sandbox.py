import uuid

from django.db import models


class SandboxMandate(models.Model):
    """Development and tests only: the provider's side of a mandate, standing in for what Remita or Lendsqr would hold. Never used with a real
    provider, and never offered in production."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey("mandates.MandateProviderConnection", on_delete=models.CASCADE, related_name="+")
    provider_ref = models.CharField(max_length=60, unique=True)
    request_ref = models.CharField(max_length=40)
    bank_code = models.CharField(max_length=12)
    account_mask = models.CharField(max_length=16, blank=True)
    maximum_minor = models.BigIntegerField()
    status = models.CharField(max_length=24, default="pending_activation")
    challenge_ref = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    debit_ready_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["connection", "request_ref"], name="sandbox_mandate_once_per_request")]


class SandboxDebit(models.Model):
    """Development and tests only: the provider's side of a debit instruction."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey("mandates.MandateProviderConnection", on_delete=models.CASCADE, related_name="+")
    mandate_ref = models.CharField(max_length=60)
    request_ref = models.CharField(max_length=40)
    provider_reference = models.CharField(max_length=60, unique=True)
    amount_minor = models.BigIntegerField()
    status = models.CharField(max_length=20, default="success")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["connection", "request_ref"], name="sandbox_debit_once_per_request")]
