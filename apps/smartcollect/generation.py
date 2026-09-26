"""Making one family's collection account, safely.

Called by a job worker, never by a request handler and never inside a transaction that holds a lock:

    1. (short transaction) check everything that must still be true - the batch is being processed and its approval still matches, the
       school's provider is still the one the batch was approved for, the family has no other live account - and mark the item as
       being generated;
    2. (no transaction) call the provider, with the item's deterministic idempotency key and the progress an earlier attempt made;
    3. (short transaction) record the account - once. The key makes step 3 idempotent, so a repeat, a double click or two workers racing
       can never make a second account for the family.

A provider that does not answer leaves the OUTCOME UNKNOWN: the request may have been carried out. The connectors look before they create
(a Paystack customer's existing dedicated account, a Monnify reference's existing reservation, a Remita order id's existing RRR), so trying
again finds what the first attempt made instead of duplicating it. After the last attempt the item is marked failed for a person to retry,
and every family that succeeded stays succeeded.
"""

import logging
from django.db import transaction
from django.utils import timezone

from apps.bankconnect import provider_connections
from apps.bankconnect.constants import ConnectionStatus
from apps.bankconnect.providers.base import (
    BadCredentials,
    ConnectorError,
    CustomerDetails,
    ProviderRejected,
    ProviderUnavailable,
    ProvisionRequest,
)
from apps.bankconnect.vault import VaultError
from apps.receivables import collection_accounts
from apps.receivables.models import AccountMode, AccountOrigin, FamilyGuardian, FamilyStatus

from . import audit, evaluation, identity, lifecycle, policy, running
from .constants import MAX_ATTEMPTS, BatchStatus, GenerationStatus
from .models import CollectionGenerationBatch, CollectionGenerationBatchItem
from .outcomes import DONE, FAILED, RETRY, Outcome

logger = logging.getLogger(__name__)


def _split(name: str) -> tuple[str, str]:
    parts = name.split()
    return (parts[0], " ".join(parts[1:])) if parts else ("", "")


def customer_of(family) -> CustomerDetails:
    """Who the account is made for: the family's primary payer as the school has them on file (the identity number is opened only here)."""
    links = list(FamilyGuardian.objects.select_related("guardian").filter(family=family, is_active=True))
    links.sort(key=lambda link: (not link.is_primary_payer, link.created_at))
    guardian = links[0].guardian if links else None
    name = guardian.name.strip() if guardian else family.display_name
    first, last = _split(name)
    held = identity.open_for_generation(family)
    return CustomerDetails(
        name=name, first_name=first, last_name=last or first, email=(guardian.email or "").strip() if guardian else "",
        phone=(guardian.phone or "").strip() if guardian else "", bvn=held.get("bvn", ""), nin=held.get("nin", ""),
    )


def _fail(item_id, code: str, message: str, *, checkpoint: dict | None = None) -> Outcome:
    with transaction.atomic():
        item = CollectionGenerationBatchItem.objects.select_for_update(of=("self",)).get(pk=item_id)
        if item.generation_status == GenerationStatus.SUCCESS:
            return Outcome(DONE)
        item.generation_status = GenerationStatus.FAILED
        item.error_code, item.safe_error_message = code[:40], message[:300]
        item.completed_at = timezone.now()
        if checkpoint is not None:
            item.checkpoint = checkpoint
        item.save()
        audit.record(item.school, "account_generation_failed", obj=item, batch=str(item.batch_id), family=str(item.family_id), code=code, attempts=item.attempt_count)
    running.refresh_progress(item.batch_id)
    return Outcome(FAILED, code)


def _again(item_id, code: str, *, checkpoint: dict) -> Outcome:
    """The provider did not answer. The item waits to be tried again, keeping the progress made."""
    with transaction.atomic():
        item = CollectionGenerationBatchItem.objects.select_for_update(of=("self",)).get(pk=item_id)
        item.generation_status = GenerationStatus.RETRYING
        item.error_code, item.safe_error_message = code, "The provider did not answer. It will be tried again."
        item.checkpoint = checkpoint
        item.save()
    return Outcome(RETRY, code)


def execute_item(item_id, *, attempt: int = 1) -> Outcome:
    # -- 1: what must still be true --------------------------------------------------------------
    with transaction.atomic():
        item = CollectionGenerationBatchItem.objects.select_related("family", "batch").select_for_update(of=("self",)).get(pk=item_id)
        if item.generation_status == GenerationStatus.SUCCESS:
            return Outcome(DONE)
        batch = CollectionGenerationBatch.objects.select_related("session", "term", "provider_connection").get(pk=item.batch_id)
        if batch.status != BatchStatus.PROCESSING or not item.selected:
            return Outcome(DONE)  # cancelled, or taken out: nothing is made
        family = item.family
        problem = None
        if not batch.approved_snapshot_hash or batch.approved_snapshot_hash != batch.snapshot_hash:
            problem = ("approval_mismatch", "This batch no longer matches what was approved.")
        connection = batch.provider_connection
        if problem is None and (not connection.is_active_provider or connection.status != ConnectionStatus.CONNECTED):
            problem = ("provider_changed", "The school's active collection provider is not the one this batch was approved for.")
        if problem is None and (family.status != FamilyStatus.ACTIVE or family.merged_into_id):
            problem = ("family_inactive", "This family is no longer active.")
        replace = None
        if problem is None:
            live = collection_accounts.live_account(family)
            if live is not None and live.idempotency_key == item.idempotency_key:
                # An earlier attempt already made and recorded it.
                return _succeed_existing(item, live)
            if live is not None:
                if live.origin == AccountOrigin.PROVIDER and live.connection_id == connection.id and (item.policy_snapshot or {}).get("existingAction") == "replace":
                    replace = live.id
                else:
                    problem = ("account_exists", "This family already has a collection account.")
        if problem is None:
            item.generation_status = GenerationStatus.GENERATING
            item.attempt_count = attempt
            item.last_attempt_at = timezone.now()
            item.save(update_fields=["generation_status", "attempt_count", "last_attempt_at", "updated_at"])
    if problem is not None:
        return _fail(item_id, *problem)

    # -- 2: the provider, with no transaction and no lock ------------------------------------------
    values = (item.policy_snapshot or {}).get("values") or {}
    scope = evaluation.account_scope(values, batch.session, batch.term, evaluation.Calendar(batch.school))
    checkpoint = dict(item.checkpoint or {})
    try:
        connector, secret = provider_connections.open_for_provider(connection)
    except VaultError:
        provider_connections.record_failure(connection, "vault_error")
        return _fail(item_id, "vault_error", "The stored credential could not be opened. Replace the provider's credentials.")
    except ConnectorError as error:
        return _fail(item_id, error.code, error.message)
    args = provider_connections.provider_call_args(connection)
    try:
        if replace is not None:
            outcome = lifecycle.retire_now(replace, connector=connector, secret=secret, args=args, reason="Replaced by a new account for the new period.")
            if outcome.result != DONE:
                return _again(item_id, outcome.code or "provider_unavailable", checkpoint=checkpoint) if outcome.result == RETRY else _fail(
                    item_id, outcome.code, "The family's earlier account could not be retired, so a new one was not made."
                )
        amount = item.proposed_collection_minor if (scope["mode"] == AccountMode.DYNAMIC or connector.info.requires_amount) else None
        valid_until = scope["valid_until"] if scope["valid_until"] and scope["valid_until"] >= timezone.localdate() else None
        request = ProvisionRequest(
            idempotency_key=item.idempotency_key, account_reference=item.provider_request_reference, family_code=family.code,
            family_name=family.display_name, customer=customer_of(family), account_name=family.display_name[:100], mode=scope["mode"],
            amount_minor=amount, valid_until=valid_until, description=f"School fees - {family.display_name}", checkpoint=checkpoint,
        )
        provisioned = connector.provision_family_collection_account(secret, request, **args)
    except ProviderUnavailable:
        if attempt >= MAX_ATTEMPTS:
            return _fail(item_id, "provider_unavailable", "The provider did not answer after several tries. Retry when it is back.", checkpoint=checkpoint)
        return _again(item_id, "provider_unavailable", checkpoint=checkpoint)
    except BadCredentials:
        provider_connections.record_failure(connection, "bad_credentials")
        return _fail(item_id, "bad_credentials", "The provider did not accept the school's credentials. Replace them, then retry.", checkpoint=checkpoint)
    except ProviderRejected as error:
        return _fail(item_id, error.code, error.message, checkpoint=checkpoint)
    except ConnectorError as error:
        return _fail(item_id, error.code, error.message, checkpoint=checkpoint)

    reference = provisioned.provider_account_ref or provisioned.account_number or provisioned.lookup_ref
    if not reference:
        return _fail(item_id, "provider_unreadable", "The provider did not name the account it made.", checkpoint=checkpoint)

    # -- 3: record it, once ---------------------------------------------------------------------------
    with transaction.atomic():
        item = CollectionGenerationBatchItem.objects.select_related("family", "batch").select_for_update(of=("self",)).get(pk=item_id)
        if item.generation_status == GenerationStatus.SUCCESS:
            return Outcome(DONE)
        account = collection_accounts.create_from_provider(
            item.family, connection, provisioned, actor=None, mode=scope["mode"], scope_session=scope["scope_session"], scope_term=scope["scope_term"],
            valid_from=scope["valid_from"], valid_until=scope["valid_until"], target_minor=item.proposed_collection_minor,
            idempotency_key=item.idempotency_key, checkpoint=checkpoint, reuse_scope=scope["reuse_scope"], reuse_count=scope["reuse_count"],
        )
        _succeed(item, account, reference)
    running.refresh_progress(item.batch_id)
    return Outcome(DONE)


def _succeed(item, account, reference: str) -> None:
    item.account = account
    item.provider_account_reference = reference[:120]
    item.generation_status = GenerationStatus.SUCCESS
    item.error_code, item.safe_error_message = "", ""
    item.completed_at = timezone.now()
    item.save()
    policy.consume_one_time(item.family)
    audit.record(
        item.school, "account_generated", obj=item, batch=str(item.batch_id), family=str(item.family_id), account=str(account.id),
        provider=account.provider, mode=account.account_mode, attempts=item.attempt_count,
    )


def _succeed_existing(item, account) -> Outcome:
    _succeed(item, account, account.external_account_ref or account.account_number)
    running.refresh_progress(item.batch_id)
    return Outcome(DONE)


def record_unexpected_failure(item_id, *, attempt: int) -> None:
    """Something in SchoolOS itself went wrong (not the provider). The item is tried again, and after the last attempt is failed for a
    person to look at; the error text is never kept, only that it happened."""
    item = CollectionGenerationBatchItem.objects.filter(pk=item_id).first()
    if item is None or item.generation_status == GenerationStatus.SUCCESS:
        return
    if attempt >= MAX_ATTEMPTS:
        _fail(item_id, "internal_error", "Something went wrong while making this account. Try again, and tell support if it keeps happening.")
    else:
        _again(item_id, "internal_error", checkpoint=dict(item.checkpoint or {}))
