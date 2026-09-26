"""Changing the school's active collection provider - on purpose, reviewed, and never by itself.

    SCHEDULED  ->  READY_TO_SWITCH  ->  APPLIED          (or CANCELLED / FAILED)

* SCHEDULED: a change of provider is planned for a date. Nothing has happened.
* READY_TO_SWITCH: the date has come and nothing stands in the way. STILL nothing has happened: SchoolOS never switches a school's provider on
  its own. A person with authority over the providers reviews it (the current and target provider, the families and accounts affected, what
  is owed, whether the new provider is ready) and applies it.
* APPLIED: the target became the ONE active provider, in a single transaction, so the school never has two or none. Batches prepared for
  the old provider are withdrawn (a batch is approved for one provider), and the accounts already made are handled as the school's switch
  policy says - retired when their family has paid, retired at once, or left for a person. Their history stays. The old provider stays
  connected while any of its accounts is live, because payments still arrive through it.

A family never has accounts with two providers: an account with the old provider is retired before the family is given one with the new.
"""

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import NotFound

from apps.bankconnect import permissions as authority
from apps.bankconnect import provider_connections
from apps.bankconnect.constants import ConnectionStatus
from apps.bankconnect.models import CollectionProviderConnection
from apps.bankconnect.providers import registry
from apps.receivables import ledger
from apps.receivables.models import LIVE_STATUSES, AccountOrigin, AccountStatus, FamilyCollectionAccount, ReceivableStatus, StudentReceivable

from . import audit, batches, lifecycle, policy
from .constants import OPEN_SWITCH, AccountMode, BatchStatus, SwitchPolicy, SwitchStatus
from .errors import CollectionRefused
from .models import CollectionGenerationBatch, ProviderSwitch


def _need_manage(membership) -> None:
    if not authority.can_manage_providers(membership):
        raise CollectionRefused("Only the owner, or someone the owner has authorised, can change the school's collection provider.", "not_provider_manager")


def get_switch(membership, switch_id, *, lock: bool = False) -> ProviderSwitch:
    query = ProviderSwitch.objects.select_related("from_connection", "to_connection").filter(school=membership.school, id=switch_id)
    switch = (query.select_for_update(of=("self",)) if lock else query).first()
    if switch is None:
        raise NotFound("That provider switch was not found.")
    return switch


def open_switch(school) -> ProviderSwitch | None:
    return ProviderSwitch.objects.select_related("from_connection", "to_connection").filter(school=school, status__in=OPEN_SWITCH).first()


def _party(connection) -> dict:
    info = registry.get_connector(connection.provider)
    return {
        "connectionId": str(connection.id), "provider": connection.provider, "providerName": info.info.display_name if info else connection.provider,
        "environment": connection.environment, "merchantName": connection.merchant_name, "status": connection.status,
        "webhookStatus": connection.webhook_status,
    }


def review(school, from_connection, to_connection) -> tuple[dict, list[str]]:
    """Everything a person needs to decide, and what still stands in the way. Reads only: no provider is called."""
    accounts = list(
        FamilyCollectionAccount.objects.filter(school=school, connection=from_connection, status__in=LIVE_STATUSES)
    )
    by_status: dict = {}
    for account in accounts:
        by_status[account.status] = by_status.get(account.status, 0) + 1
    family_ids = {a.family_id for a in accounts}
    receivables = list(StudentReceivable.objects.filter(school=school, family_id__in=family_ids).exclude(status=ReceivableStatus.VOID))
    outstanding = sum(p.outstanding for p in ledger.positions(receivables).values())
    open_batches = CollectionGenerationBatch.objects.filter(school=school, provider_connection=from_connection).exclude(
        status__in=(BatchStatus.COMPLETED, BatchStatus.PARTIALLY_SUCCESSFUL, BatchStatus.FAILED, BatchStatus.CANCELLED)
    )
    processing = open_batches.filter(status=BatchStatus.PROCESSING).count()
    blockers, warnings = [], []
    if to_connection.status != ConnectionStatus.CONNECTED:
        blockers.append(f"{to_connection.merchant_name or to_connection.provider} is not connected. Test it or replace its credentials first.")
    if from_connection.school_id != school.id or not from_connection.is_active_provider:
        blockers.append("The provider this switch was made from is no longer the active provider.")
    if processing:
        blockers.append(f"{processing} batch{'es are' if processing != 1 else ' is'} still generating accounts with the current provider. Let it finish first.")
    if to_connection.webhook_status != "active":
        warnings.append("The new provider's webhook has not been confirmed yet. Payments into its accounts will not be seen until it is set up.")
    if to_connection.environment != from_connection.environment:
        warnings.append(f"The new provider is in {to_connection.environment} mode; the current one is in {from_connection.environment} mode.")
    target = registry.get_connector(to_connection.provider)
    default_mode = policy.school_policy(school).default_account_mode
    if target is not None:
        caps = target.info.capabilities
        if not (caps.supports_static_accounts if default_mode == AccountMode.STATIC else caps.supports_dynamic_accounts):
            warnings.append(
                f"{target.info.display_name} does not issue {default_mode} accounts, and that is the school's default account type. Change the policy before preparing a batch."
            )
        if caps.requires_customer_kyc:
            warnings.append(f"{target.info.display_name} needs each family payer's BVN or NIN before it will make an account.")
    summary = {
        "current": _party(from_connection), "target": _party(to_connection),
        "affected": {"families": len(family_ids), "accounts": len(accounts), "byStatus": by_status, "outstandingMinor": outstanding},
        "batches": {"open": open_batches.count(), "processing": processing},
        "switchPolicy": policy.school_policy(school).default_provider_switch_policy,
        "warnings": warnings, "reviewedAt": timezone.now().isoformat(),
    }
    return summary, blockers


def _parse_when(value):
    if hasattr(value, "isoformat") and not isinstance(value, str):
        when = value
    else:
        when = parse_datetime(str(value or ""))
    if when is None:
        raise CollectionRefused("Say when the switch is planned for (a date and time).", "invalid_date")
    return timezone.make_aware(when) if timezone.is_naive(when) else when


@transaction.atomic
def schedule(membership, *, to_connection_id, scheduled_for, note: str = "") -> ProviderSwitch:
    """Plan a change of provider. Nothing changes: it becomes READY when its date has come, and only a person applies it."""
    _need_manage(membership)
    school = membership.school
    current = CollectionProviderConnection.objects.select_for_update().filter(school=school, is_active_provider=True).first()
    if current is None:
        raise CollectionRefused("The school has no active collection provider yet. Choose one first; a switch changes it.", "no_active_provider")
    target = CollectionProviderConnection.objects.filter(school=school, id=to_connection_id).first()
    if target is None:
        raise CollectionRefused("That provider is not connected to this school.", "connection_not_found")
    if target.id == current.id:
        raise CollectionRefused("That is already the active collection provider.", "already_active")
    if target.status != ConnectionStatus.CONNECTED:
        raise CollectionRefused("Only a connected provider can be switched to.", "not_connected")
    if registry.get_connector(target.provider) is None:
        raise CollectionRefused("That provider is not available on this server.", "provider_unavailable")
    if open_switch(school) is not None:
        raise CollectionRefused("A provider switch is already planned. Cancel it, or apply it, first.", "switch_open")
    when = _parse_when(scheduled_for)
    summary, blockers = review(school, current, target)
    try:
        with transaction.atomic():
            switch = ProviderSwitch.objects.create(
                school=school, from_connection=current, to_connection=target, scheduled_for=when, created_by=membership, review=summary,
                blockers=blockers, switch_policy=policy.school_policy(school).default_provider_switch_policy, note=" ".join(str(note or "").split())[:300],
            )
    except IntegrityError:
        raise CollectionRefused("A provider switch is already planned.", "switch_open")
    audit.record(school, "provider_switch_scheduled", actor=membership, obj=switch, to=target.provider, when=when.isoformat())
    return _advance(switch)


def _advance(switch: ProviderSwitch, now=None) -> ProviderSwitch:
    """Bring a switch's status into line with the date and what stands in the way. It never applies it."""
    now = now or timezone.now()
    if switch.status not in OPEN_SWITCH:
        return switch
    summary, blockers = review(switch.school, switch.from_connection, switch.to_connection)
    switch.review, switch.blockers = summary, blockers
    was = switch.status
    if switch.scheduled_for <= now and not blockers:
        switch.status = SwitchStatus.READY_TO_SWITCH
        if was != SwitchStatus.READY_TO_SWITCH:
            switch.ready_at = now
    else:
        switch.status = SwitchStatus.SCHEDULED
        switch.ready_at = None
    switch.save()
    if was != switch.status:
        audit.record(switch.school, "provider_switch_" + switch.status, obj=switch, blockers=len(blockers))
    return switch


def refresh(school, now=None) -> ProviderSwitch | None:
    """Look at the school's open switch again. Called when it is read, and by `manage.py refresh_provider_switches`."""
    switch = open_switch(school)
    return _advance(switch, now) if switch else None


@transaction.atomic
def cancel(membership, switch_id, *, reason: str = "") -> ProviderSwitch:
    _need_manage(membership)
    switch = get_switch(membership, switch_id, lock=True)
    if switch.status not in OPEN_SWITCH:
        raise CollectionRefused("This switch is no longer open.", "not_open")
    switch.status = SwitchStatus.CANCELLED
    switch.cancelled_by, switch.cancelled_at, switch.cancel_reason = membership, timezone.now(), " ".join(str(reason or "").split())[:300]
    switch.save()
    audit.record(membership.school, "provider_switch_cancelled", actor=membership, obj=switch, reason=switch.cancel_reason)
    return switch


def apply(membership, switch_id) -> ProviderSwitch:
    """Make the target the ONE active provider. Only a switch that is READY_TO_SWITCH, and only by a person's explicit act."""
    _need_manage(membership)
    school = membership.school
    blocked: list[str] = []
    with transaction.atomic():
        switch = get_switch(membership, switch_id, lock=True)
        if switch.status != SwitchStatus.READY_TO_SWITCH:
            raise CollectionRefused("This switch is not ready yet. It is applied only once its date has come and nothing stands in the way.", "not_ready")
        current = CollectionProviderConnection.objects.select_for_update().get(pk=switch.from_connection_id)
        target = CollectionProviderConnection.objects.select_for_update().get(pk=switch.to_connection_id)
        summary, blockers = review(school, current, target)
        if blockers:
            # What was ready no longer is: it goes back to waiting (and that is kept), and the person is told why.
            switch.review, switch.blockers, switch.status, switch.ready_at = summary, blockers, SwitchStatus.SCHEDULED, None
            switch.save()
            blocked = blockers
        else:
            provider_connections.set_active(target, actor=membership, kind="provider_switched")
            withdrawn = 0
            for batch in CollectionGenerationBatch.objects.select_for_update(of=("self",)).filter(school=school, provider_connection=current):
                if batch.status in (BatchStatus.PENDING_APPROVAL, BatchStatus.APPROVED):
                    batches._withdraw(batch, membership, "The school changed its active collection provider.")
                    withdrawn += 1
            retired = _handle_accounts(switch.switch_policy, school, current, membership)
            switch.status, switch.applied_by, switch.applied_at, switch.review, switch.blockers = (
                SwitchStatus.APPLIED, membership, timezone.now(), summary, [],
            )
            switch.save()
            audit.record(
                school, "provider_switch_applied", actor=membership, obj=switch, from_provider=current.provider, to_provider=target.provider,
                batches_withdrawn=withdrawn, accounts_retired=retired, policy=switch.switch_policy,
            )
    if blocked:
        raise CollectionRefused(blocked[0], "switch_blocked", blockers=blocked)
    return switch


def _handle_accounts(switch_policy: str, school, old_connection, membership) -> int:
    """What happens to the accounts already made with the old provider. Their history is never touched."""
    if switch_policy == SwitchPolicy.MANUAL:
        return 0
    ending = (AccountStatus.ACTIVE, AccountStatus.PROVISIONING) if switch_policy == SwitchPolicy.RETIRE_WHEN_SETTLED else ()
    retired = 0
    for account in FamilyCollectionAccount.objects.select_for_update().select_related("family", "school").filter(
        school=school, connection=old_connection, origin=AccountOrigin.PROVIDER, status__in=LIVE_STATUSES
    ):
        if account.status == AccountStatus.CLOSING or account.status in ending or account.status == AccountStatus.SUSPENDED:
            continue  # still owed on (it is retired when its family has paid), being closed already, or held by a person
        lifecycle.begin_close(account, actor=membership, reason="Retired when the school changed its collection provider.")
        retired += 1
    return retired
