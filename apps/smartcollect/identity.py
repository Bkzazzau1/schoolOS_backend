"""The identity number some providers need of a family's payer (Monnify will not reserve an account without a BVN or NIN).

It is written and never read back. It is sealed by the vault the moment it arrives, bound to this school and family (a copy in another
family's row does not open), never returned by any endpoint (a screen only learns whether one is "on file"), never logged and never put
in an audit detail. The server opens it at the instant an account is being made and for nothing else.
"""

from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from apps.bankconnect import permissions as authority
from apps.bankconnect.vault import VaultError, get_vault

from . import audit
from .errors import CollectionRefused
from .models import FamilyPayerIdentity

DIGITS = 11


def _context(family) -> dict:
    return {"school": str(family.school_id), "family": str(family.id), "purpose": "payer_identity"}


def _clean(value, what: str) -> str:
    value = "".join(str(value or "").split())
    if value and not (value.isdigit() and len(value) == DIGITS):
        raise CollectionRefused(f"{what} is {DIGITS} digits.", "invalid_identity")
    return value


def status_of(family) -> dict:
    row = FamilyPayerIdentity.objects.filter(family=family).first()
    return {"hasBvn": bool(row and row.has_bvn), "hasNin": bool(row and row.has_nin)}


@sensitive_variables("bvn", "nin", "held", "sealed")
@transaction.atomic
def save(membership, family, *, bvn="", nin="") -> FamilyPayerIdentity:
    """Record the payer's BVN and/or NIN for the family. Whoever prepares collection batches, or manages providers, may."""
    if not (authority.can_prepare(membership) or authority.can_manage_providers(membership)):
        raise CollectionRefused("Only someone who prepares collection batches, or manages the school's providers, can record a payer's identity number.", "not_preparer")
    if family.school_id != membership.school_id:
        raise CollectionRefused("That family is not at this school.", "family_not_found")
    bvn, nin = _clean(bvn, "A BVN"), _clean(nin, "A NIN")
    if not (bvn or nin):
        raise CollectionRefused("Enter a BVN or a NIN.", "identity_required")
    try:
        vault = get_vault()
    except VaultError:
        raise CollectionRefused("Secure storage is not set up on this server, so an identity number cannot be kept.", "secure_storage_unavailable")
    row = FamilyPayerIdentity.objects.select_for_update().filter(family=family).first()
    held = {}
    if row and bytes(row.sealed):
        try:
            held = vault.open(_context(family), bytes(row.sealed))
        except VaultError:
            held = {}
    if bvn:
        held["bvn"] = bvn
    if nin:
        held["nin"] = nin
    sealed = vault.seal(_context(family), held)
    if row is None:
        row = FamilyPayerIdentity(school=family.school, family=family)
    row.sealed, row.has_bvn, row.has_nin, row.updated_by = sealed, "bvn" in held, "nin" in held, membership
    row.save()
    audit.record(family.school, "payer_identity_saved", actor=membership, obj=row, family=str(family.id), has_bvn=row.has_bvn, has_nin=row.has_nin)
    return row


@sensitive_variables("held")
def open_for_generation(family) -> dict:
    """The identity numbers on file, for the moment an account is being made. `{}` if none, or if the stored value cannot be opened."""
    row = FamilyPayerIdentity.objects.filter(family=family).first()
    if row is None or not bytes(row.sealed):
        return {}
    try:
        return get_vault().open(_context(family), bytes(row.sealed))
    except VaultError:
        return {}
