"""A family's collection account and its lifecycle.

The accounting identity is the FAMILY, so a family has at most ONE live collection account per school (a database rule), however many
children it has. It is made by the school's active collection provider through Smart Money Collection (`create_from_provider`);
recording one by hand is a restricted, legacy act (`register`). Closed and failed accounts are history and stay queryable: an account
is never deleted, and a closed one still identifies its family for any payment the provider confirms into it.

The default lifecycle, applied after every change to what a family owes:

    the family owes something (published, payable, outstanding > 0)  ->  the account is ACTIVE
    the family owes nothing right now                                 ->  the account is DORMANT

A dormant account is not closed, deleted or forgotten. It keeps its provider identity, so the very same account is reused when new
fees are published and the family owes again. A school can choose a different behaviour (close, grace period, manual...) through its
collection policy (see apps.smartcollect.lifecycle). Only ACTIVE and DORMANT follow the debt automatically here; an account that is
still being set up, was suspended by a person, or is closing or closed stays as it is.

How "dormant" is enforced - the provider refusing transfers, or SchoolOS only flagging what arrives - is for a provider adapter to
decide. If a payment arrives at a dormant or closed account and the provider confirms it, SchoolOS records that outcome and reconciles
it like any other: it invents no rule of its own about the money.
"""

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.bankconnect.permissions import can_manage_providers

from . import audit, ledger
from .account_shapes import clean_details
from .errors import Refused
from .models import (
    ENDED_STATUSES,
    LIVE_STATUSES,
    AccountMode,
    AccountOrigin,
    AccountStatus,
    Family,
    FamilyCollectionAccount,
    FamilyStatus,
)
from .permissions import require_operator

_STAMPS = {
    AccountStatus.ACTIVE: "activated_at", AccountStatus.DORMANT: "dormant_at", AccountStatus.SETTLED: "settled_at", AccountStatus.CLOSED: "closed_at",
}


def live_account(family: Family) -> FamilyCollectionAccount | None:
    """The family's one live collection account at its school, if it has one."""
    return FamilyCollectionAccount.objects.filter(school=family.school, family=family, status__in=LIVE_STATUSES).first()


def _set(account: FamilyCollectionAccount, status: str, actor, why: str, *, reason: str = "") -> tuple:
    before = account.status
    now = timezone.now()
    account.status = status
    account.status_changed_at = now
    fields = ["status", "status_changed_at", "updated_at"]
    stamp = _STAMPS.get(status)
    if stamp:
        setattr(account, stamp, now)
        fields.append(stamp)
    if status == AccountStatus.CLOSED and reason:
        account.close_reason = reason[:200]
        fields.append("close_reason")
    account.save(update_fields=fields)
    audit.record(
        account.school, "collection_account_status_changed", actor=actor, obj=account, family=str(account.family_id),
        before=before, after=status, why=why,
    )
    return (account, before, status)


def sync_state(family: Family, position=None, *, actor=None) -> list[tuple]:
    """Make the family's accounts agree with what it owes. Returns `[(account, before, after)]` for those that changed."""
    position = position or ledger.family_position(family)
    changes = []
    # Includes an account left on a family that was merged into this one: it still receives this family's money.
    for account in FamilyCollectionAccount.objects.select_for_update().filter(Q(family=family) | Q(family__merged_into=family)):
        if position.outstanding > 0 and account.status == AccountStatus.DORMANT:
            changes.append(_set(account, AccountStatus.ACTIVE, actor, "The family owes money again"))
        elif position.outstanding == 0 and account.status == AccountStatus.ACTIVE:
            changes.append(_set(account, AccountStatus.DORMANT, actor, "The family has paid everything it owes"))
    return changes


def _ready_status(family: Family) -> str:
    return AccountStatus.ACTIVE if ledger.family_position(family).outstanding > 0 else AccountStatus.DORMANT


@transaction.atomic
def register(
    family: Family, *, provider: str, actor, account_number: str = "", external_account_ref: str = "",
    account_name: str = "", bank_name: str = "", connection=None, provider_meta=None, public_details=None,
    provisioned: bool = True,
) -> FamilyCollectionAccount:
    """LEGACY and RESTRICTED: record by hand an account that was made outside Smart Money Collection. It is never the normal journey - a
    family's account is made by the school's active provider (see `create_from_provider`) - so it needs the same authority as managing the
    school's providers, and it is marked as recorded by hand.

    Like every account it belongs to the FAMILY, and a family has at most one live account per school."""
    if actor is None or actor.school_id != family.school_id or not can_manage_providers(actor):
        raise Refused("Only the owner, or someone the owner has authorised, can record a family's account by hand.", "not_provider_manager")
    provider = " ".join(str(provider or "").split())
    if not provider:
        raise Refused("Say which provider gave this account.", "provider_required")
    from .account_shapes import GENERIC

    account_number = GENERIC.clean_number(account_number)
    external_account_ref = str(external_account_ref or "").strip()
    if not account_number and not external_account_ref:
        raise Refused("An account needs a number or the provider's reference for it.", "identifier_required")
    public_details = clean_details(public_details)
    if connection is not None and connection.school_id != family.school_id:
        raise Refused("That provider connection is not at this school.", "connection_not_found")
    from . import credit

    family = credit.lock_family(family)
    if live_account(family) is not None:
        raise Refused("This family already has a collection account.", "account_exists")
    try:
        with transaction.atomic():
            account = FamilyCollectionAccount.objects.create(
                school=family.school, family=family, provider=provider, connection=connection, origin=AccountOrigin.LEGACY_MANUAL,
                external_account_ref=external_account_ref, account_number=account_number, account_name=account_name[:200],
                bank_name=bank_name[:120], provider_meta=provider_meta or {}, public_details=public_details, status=AccountStatus.PROVISIONING,
            )
    except IntegrityError:
        raise Refused("That account number or reference is already in use.", "identifier_in_use")
    audit.record(family.school, "collection_account_recorded_by_hand", actor=actor, obj=account, family=str(family.id), provider=provider)
    return _provisioned(account, actor=actor, why="Provisioned") if provisioned else account


@transaction.atomic
def create_from_provider(
    family: Family, connection, provisioned, *, actor=None, mode: str = AccountMode.STATIC, scope_session=None, scope_term=None,
    valid_from=None, valid_until=None, target_minor: int | None = None, idempotency_key: str = "", checkpoint: dict | None = None,
) -> FamilyCollectionAccount:
    """Record the account the school's ACTIVE provider made for this family. Idempotent: the same `idempotency_key` gives back the account
    it made the first time, so a retry, a double click or a worker race can never make a second one.

    Refuses anything that would break an invariant: the connection must be this school's own and its active collection provider right now,
    and the family may hold no other live account."""
    from . import credit

    if connection.school_id != family.school_id:
        raise Refused("That provider connection is not at this school.", "connection_not_found")
    family = credit.lock_family(family)
    if idempotency_key:
        existing = FamilyCollectionAccount.objects.filter(school=family.school, idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing
    connection = type(connection).objects.select_for_update().get(pk=connection.pk)
    if not connection.is_active_provider:
        raise Refused("That provider is no longer the school's active collection provider.", "provider_not_active")
    if family.status != FamilyStatus.ACTIVE or family.merged_into_id:
        raise Refused("Only an active family can be given an account.", "family_inactive")
    if live_account(family) is not None:
        raise Refused("This family already has a collection account.", "account_exists")
    meta = {**(provisioned.provider_meta or {}), **(checkpoint or {})}
    try:
        with transaction.atomic():
            account = FamilyCollectionAccount.objects.create(
                school=family.school, family=family, provider=connection.provider, connection=connection, origin=AccountOrigin.PROVIDER,
                account_mode=mode, scope_session=scope_session, scope_term=scope_term, valid_from=valid_from, valid_until=valid_until,
                collection_target_minor=target_minor, idempotency_key=idempotency_key, external_account_ref=provisioned.provider_account_ref,
                account_number=provisioned.account_number, account_name=provisioned.account_name[:200], bank_name=provisioned.bank_name[:120],
                public_details=clean_details(provisioned.public_details), provider_meta=meta, status=AccountStatus.PROVISIONING,
            )
    except IntegrityError:
        raise Refused("That account number or reference is already in use.", "identifier_in_use")
    audit.record(
        family.school, "collection_account_generated", actor=actor, obj=account, family=str(family.id), provider=connection.provider,
        mode=mode, ready=provisioned.ready,
    )
    return _provisioned(account, actor=actor, why="Generated by the provider") if provisioned.ready else account


def _provisioned(account: FamilyCollectionAccount, *, actor, why: str) -> FamilyCollectionAccount:
    account = FamilyCollectionAccount.objects.select_for_update().get(pk=account.pk)
    if account.status != AccountStatus.PROVISIONING:
        return account
    _set(account, _ready_status(account.family), actor, why)
    return account


@transaction.atomic
def mark_provisioned(account: FamilyCollectionAccount, *, actor) -> FamilyCollectionAccount:
    """The provider has the account ready: it is active if the family owes money, dormant if not."""
    require_operator(actor, account.school)
    account = FamilyCollectionAccount.objects.select_for_update().get(pk=account.pk)
    if account.status != AccountStatus.PROVISIONING:
        raise Refused("This account is not being set up.", "not_provisioning")
    return _provisioned(account, actor=actor, why="Provisioned")


@transaction.atomic
def on_provider_account_event(connection, event) -> None:
    """The provider says an account it was asked to make is ready or could not be made (a Paystack dedicated-account event). Matched to the
    account SchoolOS is waiting on by the provider's own reference or number; anything that matches nothing is ignored, never guessed."""
    waiting = FamilyCollectionAccount.objects.select_for_update().filter(connection=connection, status=AccountStatus.PROVISIONING)
    account = None
    for candidate in waiting:
        if (event.account_number and candidate.account_number == event.account_number) or (
            event.reference and event.reference in (candidate.provider_meta.get("customer_code"), candidate.external_account_ref)
        ):
            account = candidate
            break
    if account is None:
        return
    if event.kind == "ready":
        _set(account, _ready_status(account.family), None, "The provider finished making the account")
    else:
        _set(account, AccountStatus.FAILED, None, "The provider could not make the account")


@transaction.atomic
def suspend(account: FamilyCollectionAccount, *, actor, reason: str) -> FamilyCollectionAccount:
    """Pause an account by hand: the family is told to check with the school before paying it. It stays paused, whatever the family owes,
    until a person reinstates it. (This is the school's own flag; it does not stop the provider accepting a transfer.)"""
    require_operator(actor, account.school)
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise Refused("Say why the account is being suspended.", "reason_required")
    account = FamilyCollectionAccount.objects.select_for_update().get(pk=account.pk)
    if account.status in (AccountStatus.SUSPENDED, *ENDED_STATUSES, AccountStatus.CLOSING):
        raise Refused("This account is already stopped.", "already_stopped")
    _set(account, AccountStatus.SUSPENDED, actor, reason)
    return account


@transaction.atomic
def reinstate(account: FamilyCollectionAccount, *, actor) -> FamilyCollectionAccount:
    require_operator(actor, account.school)
    account = FamilyCollectionAccount.objects.select_for_update().get(pk=account.pk)
    if account.status != AccountStatus.SUSPENDED:
        raise Refused("This account is not suspended.", "not_suspended")
    _set(account, _ready_status(account.family), actor, "Reinstated")
    return account


@transaction.atomic
def close(account: FamilyCollectionAccount, *, actor, reason: str) -> FamilyCollectionAccount:
    """Retire an account for good. This is not what a settled bill does (that is only dormant). The account stays on record."""
    require_operator(actor, account.school)
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise Refused("Say why the account is being closed.", "reason_required")
    account = FamilyCollectionAccount.objects.select_for_update().get(pk=account.pk)
    if account.status == AccountStatus.CLOSED:
        raise Refused("This account is already closed.", "already_closed")
    _set(account, AccountStatus.CLOSED, actor, reason, reason=reason)
    return account


def find_by_receiving_reference(school, provider: str, reference: str) -> FamilyCollectionAccount | None:
    """The family account a payment was made into, from the account reference the provider reported. A closed, dormant or failed account
    still identifies its family: an account number belongs to one family for good, and a payment the provider confirmed into it is that
    family's whatever state SchoolOS holds the account in."""
    reference = str(reference or "").strip()
    if not reference:
        return None
    accounts = FamilyCollectionAccount.objects.select_related("family").filter(school=school, provider=provider)
    return accounts.filter(account_number=reference).first() or accounts.filter(external_account_ref=reference).first()
