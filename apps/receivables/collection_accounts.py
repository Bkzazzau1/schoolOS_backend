"""A family's collection account and its lifecycle.

One rule, applied after every change to what a family owes:

    the family owes something (published, payable, outstanding > 0)  ->  the account is ACTIVE
    the family owes nothing right now                                 ->  the account is DORMANT

A dormant account is not closed, deleted or forgotten. It keeps its provider identity, so the very same
account is reused when new fees are published and the family owes again. Only ACTIVE and DORMANT follow
the debt automatically; an account that is still being set up, was suspended by a person, or was closed
stays as it is.

How "dormant" is enforced - the provider refusing transfers, or SchoolOS only flagging what arrives -
is for a provider adapter to decide. This module is provider-agnostic: it holds the status and the
receiving identifier, nothing more.
"""

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import audit, ledger
from .errors import Refused
from .models import AccountStatus, Family, FamilyCollectionAccount
from .permissions import require_operator


def _set(account: FamilyCollectionAccount, status: str, actor, why: str) -> tuple:
    before = account.status
    now = timezone.now()
    account.status = status
    account.status_changed_at = now
    if status == AccountStatus.ACTIVE:
        account.activated_at = now
    elif status == AccountStatus.DORMANT:
        account.dormant_at = now
    account.save(update_fields=["status", "status_changed_at", "activated_at", "dormant_at", "updated_at"])
    audit.record(
        account.school, "collection_account_status_changed", actor=actor, obj=account, family=str(account.family_id),
        before=before, after=status, why=why,
    )
    return (account, before, status)


def sync_state(family: Family, position=None, *, actor=None) -> list[tuple]:
    """Make the family's accounts agree with what it owes. Returns `[(account, before, after)]` for those that changed."""
    position = position or ledger.family_position(family)
    changes = []
    for account in FamilyCollectionAccount.objects.select_for_update().filter(family=family):
        if position.outstanding > 0 and account.status == AccountStatus.DORMANT:
            changes.append(_set(account, AccountStatus.ACTIVE, actor, "The family owes money again"))
        elif position.outstanding == 0 and account.status == AccountStatus.ACTIVE:
            changes.append(_set(account, AccountStatus.DORMANT, actor, "The family has paid everything it owes"))
    return changes


@transaction.atomic
def register(
    family: Family, *, provider: str, actor, account_number: str = "", external_account_ref: str = "",
    account_name: str = "", bank_name: str = "", connection=None, provider_meta=None, provisioned: bool = True,
) -> FamilyCollectionAccount:
    """Record the account a provider has given a family. With `provisioned=True` (the provider has the
    account ready) it starts ACTIVE if the family owes money and DORMANT if not; otherwise it waits in
    PROVISIONING until `mark_provisioned`."""
    require_operator(actor, family.school)
    provider = " ".join(str(provider or "").split())
    if not provider:
        raise Refused("Say which provider gave this account.", "provider_required")
    account_number, external_account_ref = str(account_number or "").strip(), str(external_account_ref or "").strip()
    if not account_number and not external_account_ref:
        raise Refused("An account needs a number or the provider's reference for it.", "identifier_required")
    if connection is not None and connection.school_id != family.school_id:
        raise Refused("That bank connection is not at this school.", "connection_not_found")
    from . import credit

    family = credit.lock_family(family)
    if FamilyCollectionAccount.objects.filter(family=family, provider=provider).exclude(status=AccountStatus.CLOSED).exists():
        raise Refused("This family already has an account with that provider.", "account_exists")
    try:
        with transaction.atomic():
            account = FamilyCollectionAccount.objects.create(
                school=family.school, family=family, provider=provider, connection=connection,
                external_account_ref=external_account_ref, account_number=account_number, account_name=account_name[:200],
                bank_name=bank_name[:120], provider_meta=provider_meta or {}, status=AccountStatus.PROVISIONING,
            )
    except IntegrityError:
        raise Refused("That account number or reference is already in use.", "identifier_in_use")
    audit.record(family.school, "collection_account_registered", actor=actor, obj=account, family=str(family.id), provider=provider)
    return mark_provisioned(account, actor=actor) if provisioned else account


@transaction.atomic
def mark_provisioned(account: FamilyCollectionAccount, *, actor) -> FamilyCollectionAccount:
    """The provider has the account ready: it is active if the family owes money, dormant if not."""
    require_operator(actor, account.school)
    account = FamilyCollectionAccount.objects.select_for_update().get(pk=account.pk)
    if account.status != AccountStatus.PROVISIONING:
        raise Refused("This account is not being set up.", "not_provisioning")
    owes = ledger.family_position(account.family).outstanding > 0
    _set(account, AccountStatus.ACTIVE if owes else AccountStatus.DORMANT, actor, "Provisioned")
    return account


@transaction.atomic
def suspend(account: FamilyCollectionAccount, *, actor, reason: str) -> FamilyCollectionAccount:
    """Stop an account by hand. It stays stopped, whatever the family owes, until a person reinstates it."""
    require_operator(actor, account.school)
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise Refused("Say why the account is being suspended.", "reason_required")
    account = FamilyCollectionAccount.objects.select_for_update().get(pk=account.pk)
    if account.status in (AccountStatus.SUSPENDED, AccountStatus.CLOSED):
        raise Refused("This account is already stopped.", "already_stopped")
    _set(account, AccountStatus.SUSPENDED, actor, reason)
    return account


@transaction.atomic
def reinstate(account: FamilyCollectionAccount, *, actor) -> FamilyCollectionAccount:
    require_operator(actor, account.school)
    account = FamilyCollectionAccount.objects.select_for_update().get(pk=account.pk)
    if account.status != AccountStatus.SUSPENDED:
        raise Refused("This account is not suspended.", "not_suspended")
    owes = ledger.family_position(account.family).outstanding > 0
    _set(account, AccountStatus.ACTIVE if owes else AccountStatus.DORMANT, actor, "Reinstated")
    return account


@transaction.atomic
def close(account: FamilyCollectionAccount, *, actor, reason: str) -> FamilyCollectionAccount:
    """Retire an account for good. This is not what a settled bill does (that is only dormant)."""
    require_operator(actor, account.school)
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise Refused("Say why the account is being closed.", "reason_required")
    account = FamilyCollectionAccount.objects.select_for_update().get(pk=account.pk)
    if account.status == AccountStatus.CLOSED:
        raise Refused("This account is already closed.", "already_closed")
    _set(account, AccountStatus.CLOSED, actor, reason)
    return account


def find_by_receiving_reference(school, provider: str, reference: str) -> FamilyCollectionAccount | None:
    """The family account a payment was made into, from the account reference the provider reported.
    An account that is closed no longer identifies anyone."""
    reference = str(reference or "").strip()
    if not reference:
        return None
    accounts = FamilyCollectionAccount.objects.select_related("family").filter(school=school, provider=provider).exclude(status=AccountStatus.CLOSED)
    return accounts.filter(account_number=reference).first() or accounts.filter(external_account_ref=reference).first()
