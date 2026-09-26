import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.receivables.models import Family, FamilyGuardian
from apps.schools.models import Membership, School

from ..constants import (
    ENDED_MANDATE,
    LIVE_MANDATE,
    MANDATE_PROVIDER_CODES,
    ConsentChannel,
    ConsentRoute,
    MandateStatus,
)
from .connection import MandateProviderConnection


class DirectDebitMandate(models.Model):
    """A payer's authority for the school to collect approved school fees from their bank account, through one provider.

    It belongs to a PAYER (a guardian in the family) and to the FAMILY, and records for good the provider connection it was made under. It is
    not a fee balance: a mandate authorises a payment RAIL, and what the family actually owes is decided by the receivables ledger alone.

    The payer's bank account is never in a readable column. Only its bank, a masked number and a keyed fingerprint are; the full number
    exists sealed (`sealed_account_details`) only while the provider needs it again to debit, and is removed when it is not needed.
    A mandate cannot be `active` without the payer's consent: the database refuses it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="direct_debit_mandates")
    family = models.ForeignKey(Family, on_delete=models.PROTECT, related_name="direct_debit_mandates")
    #: Who is authorising and paying: a guardian of the family (the guardians already on the students' records).
    payer = models.ForeignKey(FamilyGuardian, on_delete=models.PROTECT, related_name="direct_debit_mandates")
    provider_connection = models.ForeignKey(MandateProviderConnection, on_delete=models.PROTECT, related_name="mandates")
    #: What the connection was when the mandate was made (a snapshot: the mandate reads correctly whatever happens to the connection).
    provider = models.CharField(max_length=40)
    environment = models.CharField(max_length=8)

    #: The provider's own reference for the mandate, and the code it quotes when it debits, where those differ.
    provider_mandate_reference = models.CharField(max_length=120, blank=True)
    mandate_code = models.CharField(max_length=120, blank=True)
    #: The payer's id in the provider's own system, for a provider that needs one before it will make a mandate.
    provider_customer_ref = models.CharField(max_length=60, blank=True)
    #: SchoolOS's own reference for the setup call. The same on every retry of one mandate, so a retry never makes a second one.
    request_ref = models.CharField(max_length=40)

    bank_code = models.CharField(max_length=12)
    bank_name = models.CharField(max_length=120, blank=True)
    account_mask = models.CharField(max_length=16)
    account_fingerprint = models.CharField(max_length=64)
    #: The full account number, sealed by the vault, for a provider that needs it again to debit. Empty when it is not needed.
    sealed_account_details = models.BinaryField(blank=True, default=b"")
    account_details_removed_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=24, choices=MandateStatus.choices, default=MandateStatus.DRAFT)
    #: The provider's own word and code, kept so its truth is never lost in SchoolOS's simpler states.
    provider_status = models.CharField(max_length=120, blank=True)
    provider_status_code = models.CharField(max_length=20, blank=True)
    consent_route = models.CharField(max_length=10, choices=ConsentRoute.choices, default=ConsentRoute.PAYER_APP)

    #: The most that can be debited (in minor units). Its scope (each debit, or each month) is the provider's.
    maximum_amount_minor = models.BigIntegerField(null=True, blank=True)
    max_debits = models.PositiveSmallIntegerField(null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    is_primary = models.BooleanField(default=False)

    #: A summary of the consent (the durable record is `MandateConsent`).
    consent_at = models.DateTimeField(null=True, blank=True)
    consent_channel = models.CharField(max_length=16, choices=ConsentChannel.choices, blank=True)
    consent_version = models.CharField(max_length=20, blank=True)
    consent_reference = models.CharField(max_length=120, blank=True)

    activation_started_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    debit_ready_at = models.DateTimeField(null=True, blank=True)
    #: The last moment the payer can activate, when the provider says so.
    activation_deadline = models.DateTimeField(null=True, blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    #: How the payer activates, in words and facts a payer may see (never a secret or an account number).
    activation_details = models.JSONField(default=dict, blank=True)
    failure_code = models.CharField(max_length=40, blank=True)
    provider_meta = models.JSONField(default=dict, blank=True)

    created_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["school", "request_ref"], name="one_mandate_per_setup_request"),
            models.UniqueConstraint(
                fields=["provider_connection", "provider_mandate_reference"], condition=~Q(provider_mandate_reference=""),
                name="one_mandate_per_provider_reference",
            ),
            # One primary live mandate per family.
            models.UniqueConstraint(fields=["family"], condition=Q(is_primary=True) & Q(status__in=LIVE_MANDATE), name="one_primary_mandate_per_family"),
            models.CheckConstraint(condition=Q(is_primary=False) | ~Q(status__in=ENDED_MANDATE), name="an_ended_mandate_is_not_primary"),
            # A mandate is never usable without the payer's consent. Enforced by the database, not only by the service.
            models.CheckConstraint(
                condition=~Q(status__in=[MandateStatus.ACTIVE, MandateStatus.PENDING_PROVIDER_SETUP, MandateStatus.ACTIVATING]) | Q(consent_at__isnull=False),
                name="a_usable_mandate_has_consent",
            ),
            models.CheckConstraint(condition=Q(provider__in=MANDATE_PROVIDER_CODES), name="mandate_is_with_a_mandate_provider"),
        ]
        indexes = [models.Index(fields=["school", "status"]), models.Index(fields=["school", "family", "status"]), models.Index(fields=["school", "account_fingerprint"])]

    def save(self, *args, **kwargs):
        if self.family.school_id != self.school_id or self.payer.school_id != self.school_id or self.provider_connection.school_id != self.school_id:
            raise ValidationError("A mandate, its family, its payer and its provider connection must belong to the same school.")
        if self.payer.family_id != self.family_id:
            raise ValidationError("A mandate's payer must be a guardian of its family.")
        if self.provider != self.provider_connection.provider:
            raise ValidationError("A mandate's provider must be its provider connection's provider.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A mandate is never deleted: cancel it.")

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_MANDATE


class MandateConsent(models.Model):
    """The payer's consent to a mandate: durable, first-class, and never given by a member of staff on the payer's behalf.

    Either the payer, signed in as themselves, reviewed and authorised the mandate in the SchoolOS app (`payer_app`: the version and a hash of
    the exact words they saw are kept), or the provider's own authorisation is the evidence (`provider_hosted`: the provider's reference is
    kept, not a copy of what the payer did). Never edited: a withdrawal is recorded on it, and the mandate is stopped.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    mandate = models.ForeignKey(DirectDebitMandate, on_delete=models.PROTECT, related_name="consents")
    payer = models.ForeignKey(FamilyGuardian, on_delete=models.PROTECT, related_name="mandate_consents")
    consent_version = models.CharField(max_length=20)
    #: A hash of the exact text the payer was shown, so the wording can be proved later.
    consent_text_hash = models.CharField(max_length=64, blank=True)
    channel = models.CharField(max_length=16, choices=ConsentChannel.choices)
    consented_at = models.DateTimeField()
    provider_consent_reference = models.CharField(max_length=120, blank=True)
    #: A pointer to evidence kept elsewhere (never a copy of it).
    safe_evidence_reference = models.CharField(max_length=120, blank=True)
    #: The signed-in account that gave it (the payer's own), for `payer_app`.
    recorded_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawn_reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["consented_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["mandate"], condition=Q(withdrawn_at__isnull=True), name="one_standing_consent_per_mandate"),
            models.CheckConstraint(
                condition=~Q(channel=ConsentChannel.PAYER_APP) | (~Q(consent_text_hash="") & Q(recorded_by__isnull=False)),
                name="app_consent_names_the_words_and_the_payer",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.mandate.school_id != self.school_id or self.payer.family_id != self.mandate.family_id:
            raise ValidationError("A consent, its mandate and its payer must belong together.")
        if not self._state.adding:
            before = MandateConsent.objects.get(pk=self.pk)
            for name in ("consent_version", "consent_text_hash", "channel", "consented_at", "provider_consent_reference", "payer_id", "mandate_id"):
                if getattr(before, name) != getattr(self, name):
                    raise ValidationError("A consent is never edited: it can only be withdrawn.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A consent is never deleted.")


class MandateEvent(models.Model):
    """The history of a mandate: every change of state, with who and why. Append-only."""

    id = models.BigAutoField(primary_key=True)
    mandate = models.ForeignKey(DirectDebitMandate, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=32)
    from_status = models.CharField(max_length=24, blank=True)
    to_status = models.CharField(max_length=24, blank=True)
    actor = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    detail = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("A mandate event is never edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A mandate event is never deleted.")
