import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.academics.models import AcademicSession, AcademicTerm
from apps.schools.models import Membership, School

from ..constants import DEFAULT_CURRENCY
from .family import Family


class AccountStatus(models.TextChoices):
    #: Being set up with the provider. Not yet able to receive.
    PROVISIONING = "provisioning", "Being set up"
    #: The family owes money, so payments to this account are expected.
    ACTIVE = "active", "Active"
    #: The family owes nothing right now. The account is kept (not closed or deleted) and wakes up again
    #: when new fees are published.
    DORMANT = "dormant", "Dormant"
    #: Stopped by a person, or by a provider problem. Only a person brings it back.
    SUSPENDED = "suspended", "Suspended"
    CLOSED = "closed", "Closed"


class FamilyCollectionAccount(models.Model):
    """The receiving identity ONE FAMILY pays into, underneath the school's collection arrangement with a
    provider. It is not a `BankConnection` (which is the SCHOOL'S own account being connected to SchoolOS).

    The core knows nothing about how a provider implements the account. Whether "dormant" means the
    provider rejects transfers, or SchoolOS simply flags anything that arrives, is a provider adapter's
    decision; here it is only a status that follows what the family owes.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    family = models.ForeignKey(Family, on_delete=models.PROTECT, related_name="collection_accounts")
    provider = models.CharField(max_length=40)
    #: The school's connection under which the provider reports this account's payments, if any.
    connection = models.ForeignKey("bankconnect.BankConnection", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    #: The provider's own id for the account. Kept for life so the same account can be reused.
    external_account_ref = models.CharField(max_length=120, blank=True)
    #: What a payer types or transfers to, and what `BankTransaction.receiving_account_ref` carries.
    account_number = models.CharField(max_length=40, blank=True)
    account_name = models.CharField(max_length=200, blank=True)
    bank_name = models.CharField(max_length=120, blank=True)
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    status = models.CharField(max_length=14, choices=AccountStatus.choices, default=AccountStatus.PROVISIONING)
    activated_at = models.DateTimeField(null=True, blank=True)
    dormant_at = models.DateTimeField(null=True, blank=True)
    status_changed_at = models.DateTimeField(null=True, blank=True)
    #: Safe-to-show facts from the provider. Never credentials.
    provider_meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["school", "provider", "account_number"], condition=~Q(account_number=""), name="one_family_account_per_number"),
            models.UniqueConstraint(fields=["school", "provider", "external_account_ref"], condition=~Q(external_account_ref=""), name="one_family_account_per_provider_ref"),
            models.UniqueConstraint(fields=["family", "provider"], condition=~Q(status="closed"), name="one_live_account_per_family_and_provider"),
        ]
        indexes = [models.Index(fields=["school", "family", "status"])]

    def save(self, *args, **kwargs):
        if self.family.school_id != self.school_id:
            raise ValidationError("A collection account and its family must belong to the same school.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A collection account is never deleted: close it.")


class StatementStatus(models.TextChoices):
    ISSUED = "issued", "Issued"
    VOID = "void", "Void"


class FamilyStatement(models.Model):
    """A statement a school issued to a family for a session or term: its number, when, and by whom.

    The figures on a statement are ALWAYS worked out from the ledger when it is read. `snapshot` keeps a
    small record of what the totals were on the day it was issued, for reference only: it is never the
    source of truth for money, so it cannot drift from the ledger.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    family = models.ForeignKey(Family, on_delete=models.PROTECT, related_name="statements")
    session = models.ForeignKey(AcademicSession, on_delete=models.PROTECT, related_name="+")
    term = models.ForeignKey(AcademicTerm, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    number = models.CharField(max_length=40)
    status = models.CharField(max_length=8, choices=StatementStatus.choices, default=StatementStatus.ISSUED)
    issued_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    issued_at = models.DateTimeField(auto_now_add=True)
    snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-issued_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["school", "number"], name="unique_statement_number_per_school")]
        indexes = [models.Index(fields=["school", "family", "-issued_at"])]

    def save(self, *args, **kwargs):
        if self.family.school_id != self.school_id or self.session.school_id != self.school_id:
            raise ValidationError("A statement, its family and its session must belong to one school.")
        super().save(*args, **kwargs)
