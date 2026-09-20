"""Phone numbers and NINs are unique to one person in a school.

Two steps, both needed:

  * `check_available` before a write, to give a clear message naming who already
    has the number;
  * `set_claims` after the write, inside the same transaction, which the database
    then backs with a unique constraint. If two devices race, the loser's whole
    write is undone and it is told who won.
"""

from django.db import IntegrityError, transaction

from apps.core.errors import Rejected

from .models import IdentityClaim

LABELS = {"phone": "phone number", "nin": "NIN"}


class Duplicate(Rejected):
    """A phone number or NIN that already belongs to someone else."""


def _wanted(phone, nin):
    return [(kind, value) for kind, value in (("phone", phone), ("nin", nin)) if value]


def _describe(claim: IdentityClaim) -> str:
    who = claim.holder_name or claim.holder_id
    pending = " (a pending proposal)" if claim.holder_type == IdentityClaim.Holder.PROPOSAL else ""
    return f"This {LABELS[claim.kind]} is already used by {who}{pending}."


def check_available(school, holder_type: str, holder_id: str, *, phone=None, nin=None) -> None:
    """Refuse, naming the holder, if someone else already has this phone or NIN."""
    messages = []
    for kind, value in _wanted(phone, nin):
        clash = (
            IdentityClaim.objects.filter(school=school, kind=kind, value=value)
            .exclude(holder_type=holder_type, holder_id=holder_id)
            .first()
        )
        if clash:
            messages.append(_describe(clash))
    if messages:
        raise Duplicate(" ".join(messages))


@transaction.atomic
def set_claims(school, holder_type: str, holder_id: str, holder_name: str, *, phone=None, nin=None) -> None:
    """Make these the holder's numbers: claim new ones, release ones they dropped."""
    wanted = _wanted(phone, nin)
    keep = {kind for kind, _ in wanted}
    IdentityClaim.objects.filter(school=school, holder_type=holder_type, holder_id=holder_id).exclude(
        kind__in=keep
    ).delete()
    for kind, value in wanted:
        # Drop this holder's older number of the same kind, if it changed.
        IdentityClaim.objects.filter(
            school=school, holder_type=holder_type, holder_id=holder_id, kind=kind
        ).exclude(value=value).delete()
        try:
            with transaction.atomic():
                claim, created = IdentityClaim.objects.get_or_create(
                    school=school, kind=kind, value=value,
                    defaults={"holder_type": holder_type, "holder_id": holder_id, "holder_name": holder_name},
                )
        except IntegrityError:
            claim = IdentityClaim.objects.get(school=school, kind=kind, value=value)
            created = False
        if not created:
            if (claim.holder_type, claim.holder_id) != (holder_type, holder_id):
                raise Duplicate(_describe(claim))
            if claim.holder_name != holder_name:
                claim.holder_name = holder_name
                claim.save(update_fields=["holder_name"])


def release(school, holder_type: str, holder_id: str) -> None:
    IdentityClaim.objects.filter(school=school, holder_type=holder_type, holder_id=holder_id).delete()


@transaction.atomic
def transfer(school, from_type: str, from_id: str, to_type: str, to_id: str, to_name: str) -> None:
    """Hand a proposal's numbers to the staff member it became."""
    IdentityClaim.objects.filter(school=school, holder_type=from_type, holder_id=from_id).update(
        holder_type=to_type, holder_id=to_id, holder_name=to_name
    )
