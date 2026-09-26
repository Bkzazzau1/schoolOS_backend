"""What happens to a family's collection account once the family has paid everything it owes - and how an account is retired.

The school's policy decides (see `policy.SettlementAction`):

    close immediately     the account starts closing: it is retired at the provider through the job queue, then closed
    dormant immediately   the account becomes DORMANT, straight away (what SchoolOS always did before a school chose otherwise)
    wait, then dormant    SETTLED then GRACE: it waits the school's own grace period (never a number chosen here), then goes dormant
    wait, then close      the same wait, then it starts closing
    manual                it stays SETTLED until a person acts
    provider native       it stays SETTLED and SchoolOS asks the provider for nothing: the provider's own rules govern it

Nothing is ever deleted: dormant, settled and closed accounts stay on record and still identify their family for any payment the provider
confirms into them. If the family owes again while an account is dormant, settled or in its grace period, it is simply ACTIVE again.

Retiring an account is a provider call (Paystack deactivates the dedicated account, Monnify deallocates the reserved account, Remita
cancels the payment reference). It is queued with the change of state, in one transaction, and made later by a worker.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.bankconnect import provider_connections
from apps.bankconnect.constants import ConnectionStatus
from apps.bankconnect.providers.base import BadCredentials, ConnectorError, NotSupported, ProviderRejected, ProviderUnavailable
from apps.bankconnect.vault import VaultError
from apps.receivables import collection_accounts, ledger
from apps.receivables.models import AccountOrigin, AccountStatus, FamilyCollectionAccount

from . import audit, jobs, policy
from .constants import GRACE_ACTIONS, SettlementAction, SwitchPolicy
from .errors import CollectionRefused
from .outcomes import DONE, FAILED, RETRY, Outcome

logger = logging.getLogger(__name__)

AFTER_DORMANT, AFTER_CLOSE = "dormant", "close"


def policy_for(account):
    """The policy that governs this account now: the school's default, then its session, term, the batch that made it and the family's
    own override, nearest wins."""
    item = account.generation_items.select_related("batch").order_by("-created_at").first()
    return policy.resolve(
        account.school, session=account.scope_session, term=account.scope_term, batch=item.batch if item else None, family=account.family
    )


def begin_close(account, *, actor, reason: str) -> tuple:
    """Start retiring an account: it is marked closing and the provider call is queued, in one transaction."""
    before = account.status
    account = collection_accounts.mark_closing(account, actor=actor, reason=reason)
    jobs.enqueue_retire(account, reason)
    return (account, before, AccountStatus.CLOSING)


def on_settled(account, *, actor=None) -> list[tuple]:
    """The family owes nothing any more: carry out the school's policy. Returns `[(account, before, after)]`."""
    resolved = policy_for(account)
    action = resolved["settlement_action"]
    now = timezone.now()
    why = "The family has paid everything it owes"
    connection = account.connection
    if (
        connection is not None and not connection.is_active_provider and account.origin == AccountOrigin.PROVIDER
        and policy.school_policy(account.school).default_provider_switch_policy == SwitchPolicy.RETIRE_WHEN_SETTLED
    ):
        # The school has moved to another provider and chose to keep each earlier account until its family had paid: they have.
        return [begin_close(account, actor=actor, reason="Settled after the school changed its collection provider.")]
    if action == SettlementAction.CLOSE_IMMEDIATELY:
        return [begin_close(account, actor=actor, reason="Settled: the school's policy closes an account once its family has paid.")]
    if action in GRACE_ACTIONS:
        hours = resolved.get("grace_period_hours")
        if hours:
            after = AFTER_CLOSE if action == SettlementAction.GRACE_THEN_CLOSE else AFTER_DORMANT
            return [collection_accounts.set_state(
                account, AccountStatus.GRACE, actor=actor, why=why + "; waiting out the school's grace period",
                grace_until=now + timedelta(hours=hours), after_grace=after,
            )]
        # A wait with no waiting period is a policy that was never finished. Nothing is closed on a guess: the account is left for a person.
        audit.record(account.school, "policy_incomplete_at_settlement", obj=account, action=str(action))
        return [collection_accounts.set_state(account, AccountStatus.SETTLED, actor=actor, why=why + ", but the policy has no waiting period")]
    if action in (SettlementAction.MANUAL, SettlementAction.PROVIDER_NATIVE):
        return [collection_accounts.set_state(account, AccountStatus.SETTLED, actor=actor, why=why)]
    return [collection_accounts.set_state(account, AccountStatus.DORMANT, actor=actor, why=why)]


def process_grace(now=None) -> int:
    """Carry on with every account whose grace period has ended. A family that owes again is simply active again. Returns how many moved."""
    now = now or timezone.now()
    moved = 0
    for pk in FamilyCollectionAccount.objects.filter(status=AccountStatus.GRACE, grace_until__lte=now).values_list("pk", flat=True):
        with transaction.atomic():
            account = FamilyCollectionAccount.objects.select_for_update().select_related("family", "school").get(pk=pk)
            if account.status != AccountStatus.GRACE or not account.grace_until or account.grace_until > now:
                continue
            position = ledger.family_position(account.family)
            if position.outstanding > 0:
                collection_accounts.set_state(account, AccountStatus.ACTIVE, actor=None, why="The family owes money again")
            elif account.after_grace == AFTER_CLOSE:
                begin_close(account, actor=None, reason="Settled and the school's grace period ended: the policy closes the account.")
            else:
                collection_accounts.set_state(account, AccountStatus.DORMANT, actor=None, why="The grace period ended")
            moved += 1
    return moved


# -- retiring at the provider ---------------------------------------------------------------------------


@transaction.atomic
def retire(membership, account_id, *, reason: str) -> FamilyCollectionAccount:
    """A person retires a family's account: it starts closing and the provider is asked to close it. (An account recorded by hand has no
    provider to ask and is closed at once.)"""
    from apps.receivables.permissions import require_operator

    account = FamilyCollectionAccount.objects.select_for_update().select_related("family", "school").filter(school=membership.school, pk=account_id).first()
    if account is None:
        raise CollectionRefused("That collection account was not found.", "account_not_found")
    require_operator(membership, membership.school)
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise CollectionRefused("Say why the account is being retired.", "reason_required")
    if account.status in (AccountStatus.CLOSING, AccountStatus.CLOSED, AccountStatus.FAILED):
        raise CollectionRefused("This account is already closing or closed.", "already_closed")
    if account.connection_id is None:
        collection_accounts.mark_closed(account, actor=membership, reason=reason)
    else:
        begin_close(account, actor=membership, reason=reason)
    audit.record(account.school, "account_retire_requested", actor=membership, obj=account, family=str(account.family_id), reason=reason)
    return account


def retire_now(account_id, *, connector, secret, args, reason: str) -> Outcome:
    """Ask the provider to retire one account, with a connector already opened. Called by a worker (or by generation, which retires an
    account it is replacing). Never inside a transaction that holds a lock."""
    account = FamilyCollectionAccount.objects.select_related("school", "family").get(pk=account_id)
    if account.status == AccountStatus.CLOSED:
        return Outcome(DONE)
    collection_accounts.mark_closing(account, actor=None, reason=reason)
    note = reason
    try:
        if connector.info.capabilities.supports_account_closure and account.external_account_ref:
            connector.close_collection_account(secret, account_ref=account.external_account_ref, **args)
    except ProviderUnavailable:
        return Outcome(RETRY, "provider_unavailable")
    except BadCredentials:
        provider_connections.record_failure(account.connection, "bad_credentials")
        return Outcome(FAILED, "bad_credentials")
    except ProviderRejected:
        # The provider answered and would not cancel it (a Remita reference that was already paid, an account it no longer has).
        # It stays a closed account of ours, with that said, and still identifies its family for anything the provider confirms into it.
        note = "The provider did not confirm the cancellation (the account may already be used or closed). " + reason
    except NotSupported:
        pass
    except ConnectorError as error:
        return Outcome(FAILED, error.code)
    collection_accounts.mark_closed(account, actor=None, reason=note)
    audit.record(account.school, "account_retired", obj=account, family=str(account.family_id), provider=account.provider)
    return Outcome(DONE)


def execute_retire(account_id, *, attempt: int = 1) -> Outcome:
    account = FamilyCollectionAccount.objects.select_related("connection", "school").filter(pk=account_id).first()
    if account is None or account.status in (AccountStatus.CLOSED, AccountStatus.FAILED):
        return Outcome(DONE)
    reason = account.close_reason or "Retired."
    connection = account.connection
    if connection is None or connection.status == ConnectionStatus.REVOKED:
        collection_accounts.mark_closed(account, actor=None, reason=reason)
        return Outcome(DONE)
    try:
        connector, secret = provider_connections.open_for_provider(connection)
    except (VaultError, ConnectorError):
        return Outcome(FAILED, "vault_error")
    return retire_now(account_id, connector=connector, secret=secret, args=provider_connections.provider_call_args(connection), reason=reason)
