from django.db import models

from apps.schools.models import School


class IdentityClaim(models.Model):
    """One phone number or NIN, and the one person or proposal that holds it.

    A phone number and a NIN each belong to exactly one person in a school. The
    database enforces that (the unique constraint below), so two devices working
    offline cannot both register the same person: whichever syncs second is told
    who already has it.

    A pending proposal holds its numbers too, so the same person cannot be proposed
    twice. Approving moves the claim from the proposal to the new staff member;
    rejecting releases it.
    """

    class Kind(models.TextChoices):
        PHONE = "phone"
        NIN = "nin"

    class Holder(models.TextChoices):
        PROPOSAL = "proposal"
        STAFF = "staff"

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="identity_claims")
    kind = models.CharField(max_length=5, choices=Kind.choices)
    value = models.CharField(max_length=20)
    holder_type = models.CharField(max_length=10, choices=Holder.choices)
    holder_id = models.CharField(max_length=128)
    #: Shown in "already used by ...", so it is kept with the claim.
    holder_name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["school", "kind", "value"], name="one_holder_per_number"),
        ]
        indexes = [models.Index(fields=["school", "holder_type", "holder_id"])]

    def __str__(self):
        return f"{self.kind} {self.value} -> {self.holder_type} {self.holder_id}"
