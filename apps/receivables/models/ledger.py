import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.schools.models import Membership, School

from ..constants import DEFAULT_CURRENCY
from .family import Family
from .fees import StudentReceivable


class CreditKind(models.TextChoices):
    # -- money the family is owed (adds to its credit)
    #: Payment received beyond what the family owed. Never silently dropped and never counted as earned fees.
    OVERPAYMENT = "overpayment", "Overpayment"
    #: A charge needed less than had been put towards it (a scholarship came after the payment, or a charge was voided).
    RELEASED = "released", "Released from a charge"
    #: A use of credit was undone, so the credit is back.
    APPLICATION_REVERSED = "application_reversed", "Application reversed"
    # -- money taken out of the family's credit
    #: Credit used to pay a charge.
    APPLIED = "applied", "Applied to a charge"
    #: Credit paid back to the family.
    REFUNDED = "refunded", "Refunded"
    #: The payment the credit came from was reversed, so the credit is gone.
    OVERPAYMENT_REVERSED = "overpayment_reversed", "Payment reversed"


#: Kinds that add to a family's credit, and kinds that use it up.
CREDIT_IN = (CreditKind.OVERPAYMENT, CreditKind.RELEASED, CreditKind.APPLICATION_REVERSED)
CREDIT_OUT = (CreditKind.APPLIED, CreditKind.REFUNDED, CreditKind.OVERPAYMENT_REVERSED)


class FamilyCreditEntry(models.Model):
    """One movement of a family's credit. The credit balance is the sum of these and nothing else, so it
    cannot drift. Rows are never edited or deleted: an application that must be undone is undone by a
    `application_reversed` row, so the history of what happened stays on record.

    Amounts are positive; the kind says whether credit went up or down.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    family = models.ForeignKey(Family, on_delete=models.PROTECT, related_name="credit_entries")
    kind = models.CharField(max_length=24, choices=CreditKind.choices)
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    #: The bank payment this credit came from (or, for a reversal, the one that was reversed).
    transaction = models.ForeignKey("bankconnect.BankTransaction", null=True, blank=True, on_delete=models.PROTECT, related_name="credit_entries")
    #: The charge it was applied to (or taken back from).
    receivable = models.ForeignKey(StudentReceivable, null=True, blank=True, on_delete=models.PROTECT, related_name="credit_entries")
    reverses = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversal")
    reason = models.CharField(max_length=300, blank=True)
    actor = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    #: A key for entries that must happen once however many times they are asked for.
    ref = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount_minor__gt=0), name="credit_entry_amount_positive"),
            models.UniqueConstraint(fields=["school", "ref"], condition=~Q(ref=""), name="one_credit_entry_per_ref"),
        ]
        indexes = [
            models.Index(fields=["school", "family", "kind"]),
            models.Index(fields=["transaction", "kind"]),
            models.Index(fields=["receivable", "kind"]),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("A credit entry is never edited: add a reversing entry.")
        if self.family.school_id != self.school_id:
            raise ValidationError("A credit entry and its family must belong to the same school.")
        if self.receivable_id and self.receivable.family_id != self.family_id:
            raise ValidationError("A credit entry can only touch a charge of its own family.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A credit entry is never deleted.")
