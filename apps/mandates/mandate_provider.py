"""What happens between SchoolOS and the provider over a mandate's life: asking it to make the mandate, the payer's activation, asking where it
stands, and stopping it.

Every provider call is made WITHOUT a transaction open and without a row lock held: the mandate is read (and, where it must be, claimed) in one short
transaction, the provider is asked, and the answer is written in another. The provider's answer is what moves a mandate on; a payer's word or a
staff member's never does. A mandate is never called ACTIVE because the payer finished one step: only when the provider says it can be debited.
"""

import logging
from dataclasses import replace

from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from rest_framework.exceptions import NotFound

from apps.schools.models import Role

from . import audit, consent, notifications, permissions
from .connections import open_for_provider, provider_call_args, record_failure, record_success
from .constants import ENDED_MANDATE, ConsentRoute, MandateStatus
from .errors import MandateRefused
from .mandates import get_mandate, record_event
from .models import DirectDebitMandate
from .providers import registry
from .providers.base import (
    BadCredentials,
    ConnectorError,
    CreateMandateRequest,
    PayerDetails,
    ProviderMandate,
    ProviderRejected,
    ProviderUnavailable,
)
from .vault import VaultError, account_context_for, get_vault

logger = logging.getLogger(__name__)

#: A mandate the provider still has something to say about.
WATCHED = (
    MandateStatus.PENDING_ACTIVATION, MandateStatus.ACTIVATING, MandateStatus.PENDING_PROVIDER_SETUP, MandateStatus.ACTIVE, MandateStatus.SUSPENDED,
)
_USABLE = (MandateStatus.ACTIVATING, MandateStatus.PENDING_PROVIDER_SETUP, MandateStatus.ACTIVE)
#: Purpose of each state change on the audit trail.
_AUDIT = {
    MandateStatus.PENDING_PROVIDER_SETUP: "mandate_activated", MandateStatus.ACTIVE: "mandate_debit_ready", MandateStatus.SUSPENDED: "mandate_suspended",
    MandateStatus.CANCELLED: "mandate_cancelled", MandateStatus.FAILED: "mandate_failed", MandateStatus.EXPIRED: "mandate_expired",
    MandateStatus.ACTIVATING: "activation_started",
}


def _connector_of(mandate):
    connector = registry.get_connector(mandate.provider)
    if connector is None:
        raise MandateRefused("This provider is not available on this server.", "provider_unavailable")
    return connector


def _hints(mandate) -> dict:
    return {"request_ref": mandate.request_ref, "provider_customer_ref": mandate.provider_customer_ref, "bank_code": mandate.bank_code}


def _purge_account(mandate, now) -> None:
    if mandate.sealed_account_details:
        mandate.sealed_account_details = b""
        mandate.account_details_removed_at = now


# -- applying what the provider says ------------------------------------------------------------------


def apply_provider_state(mandate, provider_mandate: ProviderMandate, *, actor=None, source: str = "provider") -> bool:
    """Bring the mandate into line with what the provider says. Call it inside a transaction that holds the mandate's row. Returns whether the
    state changed. An ended mandate is never revived, and a suspension SchoolOS made itself (for a provider with no pause) is not undone by the
    provider still calling the mandate active."""
    if mandate.status in ENDED_MANDATE:
        return False
    caps = _connector_of(mandate).info.capabilities
    now = timezone.now()
    old, new = mandate.status, provider_mandate.status
    if old == MandateStatus.SUSPENDED and not caps.supports_suspension and new in _USABLE + (MandateStatus.PENDING_ACTIVATION,):
        new = MandateStatus.SUSPENDED
    if new in _USABLE and not mandate.consent_at:
        consent.record_provider_consent(mandate, reference=provider_mandate.provider_ref, at=provider_mandate.activated_at)
    mandate.provider_mandate_reference = provider_mandate.provider_ref or mandate.provider_mandate_reference
    mandate.mandate_code = provider_mandate.mandate_code or mandate.mandate_code
    mandate.provider_status, mandate.provider_status_code = provider_mandate.provider_status, provider_mandate.provider_status_code
    mandate.provider_meta = {**(mandate.provider_meta or {}), **provider_mandate.meta}
    details = dict(mandate.activation_details or {})
    for key, value in (provider_mandate.activation or {}).items():
        details[key] = value
    mandate.activation_details = details
    mandate.activation_deadline = provider_mandate.activation_deadline or mandate.activation_deadline
    mandate.start_date = provider_mandate.start_date or mandate.start_date
    mandate.end_date = provider_mandate.end_date or mandate.end_date
    if new in (MandateStatus.PENDING_ACTIVATION, MandateStatus.ACTIVATING) and not mandate.activation_started_at:
        mandate.activation_started_at = now
    if new in (MandateStatus.PENDING_PROVIDER_SETUP, MandateStatus.ACTIVE) and not mandate.activated_at:
        mandate.activated_at = provider_mandate.activated_at or now
    if new == MandateStatus.ACTIVE:
        mandate.debit_ready_at = mandate.debit_ready_at or provider_mandate.debit_ready_at or now
    elif new == MandateStatus.PENDING_PROVIDER_SETUP and provider_mandate.debit_ready_at:
        mandate.debit_ready_at = provider_mandate.debit_ready_at
    stamp = {MandateStatus.SUSPENDED: "suspended_at", MandateStatus.CANCELLED: "cancelled_at", MandateStatus.EXPIRED: "expired_at", MandateStatus.FAILED: "failed_at"}.get(new)
    if stamp and old != new:
        setattr(mandate, stamp, now)
    if new in ENDED_MANDATE:
        mandate.is_primary = False
        if new == MandateStatus.CANCELLED:
            consent.withdraw(mandate, reason="The mandate was cancelled.")
        _purge_account(mandate, now)
    elif new in _USABLE and not caps.requires_account_number_for_debit:
        _purge_account(mandate, now)
    mandate.status = new
    mandate.failure_code = "" if new not in (MandateStatus.FAILED,) else mandate.failure_code
    mandate.last_synced_at = now
    mandate.save()
    if new != old:
        record_event(mandate, "status_changed", actor=actor, from_status=old, to_status=new, source=source, provider_status=provider_mandate.provider_status)
        audit.record(mandate.school, _AUDIT.get(new, "mandate_status_changed"), actor=actor, obj=mandate, was=old, now=new, source=source)
        _tell_about(mandate, new)
    return new != old


def _tell_about(mandate, new: str) -> None:
    if new == MandateStatus.PENDING_ACTIVATION:
        notifications.needs_activation(mandate)
    elif new == MandateStatus.ACTIVE:
        notifications.activated(mandate)
    elif new == MandateStatus.CANCELLED:
        notifications.cancelled(mandate)
    elif new in (MandateStatus.FAILED, MandateStatus.EXPIRED):
        notifications.problem(mandate, f"the mandate on {mandate.account_mask} {new}.")


# -- asking the provider to make it -------------------------------------------------------------------


@sensitive_variables("account", "sealed")
def send_to_provider(mandate_id, *, actor=None) -> DirectDebitMandate:
    """Ask the provider to make the mandate. Safe to repeat: the setup call carries the mandate's own reference, and a mandate that already
    has the provider's reference is only asked about. Never called for an app-route mandate the payer has not authorised."""
    with transaction.atomic():
        mandate = DirectDebitMandate.objects.select_for_update(of=("self",)).select_related("family", "payer__guardian", "provider_connection", "school").get(id=mandate_id)
        if mandate.status not in (MandateStatus.DRAFT, MandateStatus.PENDING_CONSENT):
            return mandate
        if mandate.consent_route == ConsentRoute.PAYER_APP and not mandate.consent_at:
            raise MandateRefused("The payer has not authorised this mandate yet.", "payer_consent_required")
        if not mandate.sealed_account_details:
            raise MandateRefused("This mandate's bank details are no longer held. Start it again.", "account_details_missing")
        connection, guardian = mandate.provider_connection, mandate.payer.guardian
        request = CreateMandateRequest(
            request_ref=mandate.request_ref, payer=PayerDetails(name=guardian.name, email=guardian.email, phone=guardian.phone), bank_code=mandate.bank_code,
            account_number="", maximum_amount_minor=mandate.maximum_amount_minor, start_date=mandate.start_date, end_date=mandate.end_date,
            max_debits=mandate.max_debits, description="School fees", provider_customer_ref=mandate.provider_customer_ref,
            existing_ref=mandate.provider_mandate_reference,
        )
        sealed = bytes(mandate.sealed_account_details)
    # -- the provider is asked here, with nothing held ----------------------------------------------
    try:
        connector, secret = open_for_provider(connection)
        account = get_vault().open(account_context_for(mandate), sealed)["account_number"]
        request = replace(request, account_number=account)
        provider_mandate = connector.create_mandate(secret, request, **provider_call_args(connection))
    except BadCredentials:
        record_failure(connection, "bad_credentials")
        return _setup_failed(mandate_id, "bad_credentials", "The school's credentials were refused by the provider.", retryable=True, actor=actor)
    except ProviderUnavailable:
        return _setup_failed(mandate_id, "provider_unavailable", "The provider did not answer.", retryable=True, actor=actor)
    except (ProviderRejected, ConnectorError) as error:
        return _setup_failed(mandate_id, error.code, error.message, retryable=False, actor=actor)
    except VaultError:
        return _setup_failed(mandate_id, "vault_error", "The stored details could not be opened.", retryable=False, actor=actor)
    record_success(connection)
    with transaction.atomic():
        mandate = get_locked(mandate_id)
        mandate.failure_code = ""
        apply_provider_state(mandate, provider_mandate, actor=actor, source="setup")
        audit.record(mandate.school, "activation_instructions_ready", actor=actor, obj=mandate)
    return mandate


def get_locked(mandate_id) -> DirectDebitMandate:
    return DirectDebitMandate.objects.select_for_update(of=("self",)).select_related("family", "payer__guardian", "provider_connection", "school").get(id=mandate_id)


def _setup_failed(mandate_id, code: str, message: str, *, retryable: bool, actor) -> DirectDebitMandate:
    with transaction.atomic():
        mandate = get_locked(mandate_id)
        mandate.failure_code = code[:40]
        old = mandate.status
        if not retryable:
            mandate.status, mandate.failed_at, mandate.is_primary = MandateStatus.FAILED, timezone.now(), False
            _purge_account(mandate, timezone.now())
        mandate.save()
        if mandate.status != old:
            record_event(mandate, "status_changed", actor=actor, from_status=old, to_status=mandate.status, code=code)
        audit.record(mandate.school, "mandate_setup_failed", actor=actor, obj=mandate, code=code, retryable=retryable)
    if not retryable:
        notifications.problem(mandate, "the provider could not make the mandate. " + message)
    return mandate


def retry_setup(membership, mandate_id) -> DirectDebitMandate:
    """Ask the provider again for a mandate whose setup did not go through (the provider did not answer, or refused the school's credentials)."""
    if not permissions.can_manage(membership):
        raise MandateRefused("Only the owner, or someone the owner has authorised, can manage mandates.", "not_mandate_manager")
    mandate = get_mandate(membership, mandate_id)
    if mandate.status != MandateStatus.DRAFT or mandate.consent_route == ConsentRoute.PAYER_APP and not mandate.consent_at:
        raise MandateRefused("This mandate is not waiting to be sent to the provider.", "not_retryable")
    return send_to_provider(mandate.id, actor=membership)


# -- the payer -----------------------------------------------------------------------------------------


def payer_mandate(membership, mandate_id, *, lock: bool = False) -> DirectDebitMandate:
    """A mandate that belongs to THIS signed-in payer. Anyone else's looks like it does not exist."""
    if membership.role != Role.PARENT:
        raise NotFound("That mandate was not found.")
    query = DirectDebitMandate.objects.select_related("family", "payer__guardian", "provider_connection", "school").filter(
        school=membership.school, id=mandate_id, payer__guardian__account_user_id=membership.user_id,
    )
    mandate = (query.select_for_update(of=("self",)) if lock else query).first()
    if mandate is None:
        raise NotFound("That mandate was not found.")
    return mandate


def payer_consent(membership, mandate_id, *, shown_hash: str, accepted) -> DirectDebitMandate:
    """The payer authorises the mandate, themselves. Only then is the provider asked to make it."""
    if accepted is not True:
        raise MandateRefused("Authorising means saying yes to what it states.", "consent_not_given")
    with transaction.atomic():
        mandate = payer_mandate(membership, mandate_id, lock=True)
        if mandate.status != MandateStatus.PENDING_CONSENT:
            raise MandateRefused("This mandate is not waiting for your authorisation.", "not_pending_consent")
        consent.record_payer_consent(mandate, membership, shown_hash=shown_hash)
        mandate.save(update_fields=["consent_at", "consent_channel", "consent_version", "consent_reference", "updated_at"])
        record_event(mandate, "consent_recorded", actor=membership, channel=mandate.consent_channel, version=mandate.consent_version)
        audit.record(mandate.school, "consent_recorded", actor=membership, obj=mandate, channel=mandate.consent_channel, version=mandate.consent_version)
    return send_to_provider(mandate.id, actor=membership)


@sensitive_variables("answers")
def request_activation(membership, mandate_id) -> dict:
    """Start the payer's activation in the app (the bank sends them a one-time password). Returns what they are asked to type."""
    mandate = payer_mandate(membership, mandate_id)
    connector = _connector_of(mandate)
    if mandate.status != MandateStatus.PENDING_ACTIVATION or not connector.info.capabilities.supports_otp_activation:
        raise MandateRefused("This mandate cannot be activated this way.", "cannot_activate_here")
    connection = mandate.provider_connection
    try:
        _, secret = open_for_provider(connection)
        bank = next((b for b in connector.list_supported_banks(secret, **provider_call_args(connection)) if b.code == mandate.bank_code), None)
        if bank is not None and not bank.self_activation:
            raise MandateRefused("Your bank does not activate this way. Use the mandate form and take it to your bank.", "bank_needs_form")
        challenge = connector.request_activation(secret, mandate_ref=mandate.provider_mandate_reference, **provider_call_args(connection), **_hints(mandate))
    except BadCredentials:
        record_failure(connection, "bad_credentials")
        raise MandateRefused("The school's connection to the provider needs attention. Ask the school.", "provider_not_working")
    except ProviderUnavailable:
        raise MandateRefused("The provider did not answer. Try again in a moment.", "provider_unavailable")
    except ConnectorError as error:
        raise MandateRefused(error.message, error.code)
    with transaction.atomic():
        locked = get_locked(mandate.id)
        locked.activation_details = {**(locked.activation_details or {}), "challengeRef": challenge.challenge_ref, "challengeFields": list(challenge.fields)}
        locked.save(update_fields=["activation_details", "updated_at"])
    return {"fields": list(challenge.fields)}


@sensitive_variables("answers")
def confirm_activation(membership, mandate_id, *, answers: dict) -> DirectDebitMandate:
    """The payer types what the bank asked for (a one-time password). It goes to the provider and is not kept anywhere."""
    if not isinstance(answers, dict) or not answers or any(not str(v or "").strip() for v in answers.values()):
        raise MandateRefused("Enter everything the bank asked for.", "answers_required")
    mandate = payer_mandate(membership, mandate_id)
    connector = _connector_of(mandate)
    challenge_ref = str((mandate.activation_details or {}).get("challengeRef") or "")
    if mandate.status != MandateStatus.PENDING_ACTIVATION or not challenge_ref:
        raise MandateRefused("Ask for a one-time password first.", "no_challenge")
    connection = mandate.provider_connection
    try:
        _, secret = open_for_provider(connection)
        provider_mandate = connector.confirm_activation(
            secret, mandate_ref=mandate.provider_mandate_reference, challenge_ref=challenge_ref, answers={k: str(v).strip() for k, v in answers.items()},
            **provider_call_args(connection), **_hints(mandate),
        )
    except BadCredentials:
        record_failure(connection, "bad_credentials")
        raise MandateRefused("The school's connection to the provider needs attention. Ask the school.", "provider_not_working")
    except ProviderUnavailable:
        raise MandateRefused("The provider did not answer. Check the mandate's status before trying again.", "provider_unavailable")
    except ConnectorError as error:
        raise MandateRefused(error.message, error.code)
    with transaction.atomic():
        mandate = get_locked(mandate.id)
        details = {k: v for k, v in (mandate.activation_details or {}).items() if k not in ("challengeRef", "challengeFields")}
        mandate.activation_details = details
        apply_provider_state(mandate, provider_mandate, actor=membership, source="payer_activation")
    return mandate


# -- asking where it stands ---------------------------------------------------------------------------


def refresh_mandate(mandate_id, *, actor=None, source: str = "refresh", strict: bool = False) -> DirectDebitMandate:
    """Ask the provider where the mandate stands and bring it into line. A provider that does not answer changes nothing (with `strict`, it is
    raised, for a caller that must be able to say "try again": a callback)."""
    mandate = DirectDebitMandate.objects.select_related("provider_connection", "family", "payer__guardian", "school").get(id=mandate_id)
    if not mandate.provider_mandate_reference or mandate.status not in WATCHED:
        return mandate
    connection = mandate.provider_connection
    try:
        connector, secret = open_for_provider(connection)
        provider_mandate = connector.get_mandate_status(secret, mandate_ref=mandate.provider_mandate_reference, **provider_call_args(connection), **_hints(mandate))
    except BadCredentials:
        record_failure(connection, "bad_credentials")
        if strict:
            raise
        return mandate
    except (ConnectorError, VaultError) as error:
        record_failure(connection, getattr(error, "code", "vault_error"))
        if strict:
            raise ProviderUnavailable() from None
        return mandate
    record_success(connection)
    with transaction.atomic():
        mandate = get_locked(mandate_id)
        apply_provider_state(mandate, provider_mandate, actor=actor, source=source)
    return mandate


def refresh(membership, mandate_id) -> DirectDebitMandate:
    if permissions.can_view(membership):
        mandate = get_mandate(membership, mandate_id)
    else:
        mandate = payer_mandate(membership, mandate_id)
    return refresh_mandate(mandate.id, actor=membership)


def resend_activation(membership, mandate_id) -> DirectDebitMandate:
    if not permissions.can_manage(membership):
        raise MandateRefused("Only the owner, or someone the owner has authorised, can manage mandates.", "not_mandate_manager")
    mandate = get_mandate(membership, mandate_id)
    if mandate.status == MandateStatus.PENDING_CONSENT:
        notifications.needs_authorisation(mandate)
    elif mandate.status == MandateStatus.PENDING_ACTIVATION:
        notifications.needs_activation(mandate)
    else:
        raise MandateRefused("This mandate is not waiting for the payer.", "not_waiting_for_payer")
    audit.record(mandate.school, "activation_instructions_resent", actor=membership, obj=mandate)
    return mandate


# -- stopping it --------------------------------------------------------------------------------------


def _provider_stop(mandate, operation: str):
    """Make a provider stop-type call and return what it says, or raise a refusal a person can act on."""
    connector = _connector_of(mandate)
    connection = mandate.provider_connection
    try:
        _, secret = open_for_provider(connection)
        return getattr(connector, operation)(secret, mandate_ref=mandate.provider_mandate_reference, **provider_call_args(connection), **_hints(mandate))
    except BadCredentials:
        record_failure(connection, "bad_credentials")
        raise MandateRefused("The school's connection to the provider needs attention, so the mandate could not be changed there.", "provider_not_working")
    except ProviderUnavailable:
        raise MandateRefused("The provider did not answer, so nothing was changed. Try again in a moment.", "provider_unavailable")
    except ConnectorError as error:
        raise MandateRefused(error.message, error.code)


def _stop(mandate_id, membership, *, reason: str, source: str) -> DirectDebitMandate:
    mandate = DirectDebitMandate.objects.select_related("provider_connection", "family", "payer__guardian", "school").get(id=mandate_id)
    if mandate.status in ENDED_MANDATE:
        return mandate
    caps = _connector_of(mandate).info.capabilities
    provider_mandate = None
    if mandate.provider_mandate_reference:
        if not caps.supports_remote_cancellation:
            raise MandateRefused("This provider cannot cancel a mandate through SchoolOS. Cancel it with the provider.", "provider_cannot_cancel")
        provider_mandate = _provider_stop(mandate, "cancel_mandate")
    with transaction.atomic():
        mandate = get_locked(mandate_id)
        if mandate.status in ENDED_MANDATE:
            return mandate
        if provider_mandate is not None and provider_mandate.status != MandateStatus.CANCELLED:
            provider_mandate = replace(provider_mandate, status=MandateStatus.CANCELLED)
        elif provider_mandate is None:
            provider_mandate = ProviderMandate(provider_ref="", status=MandateStatus.CANCELLED, provider_status="cancelled before it reached the provider")
        apply_provider_state(mandate, provider_mandate, actor=membership, source=source)
        audit.record(mandate.school, "mandate_cancelled_by", actor=membership, obj=mandate, reason=" ".join(str(reason or "").split())[:300], source=source)
    return mandate


def cancel(membership, mandate_id, *, reason: str = "") -> DirectDebitMandate:
    if not permissions.can_manage(membership):
        raise MandateRefused("Only the owner, or someone the owner has authorised, can manage mandates.", "not_mandate_manager")
    mandate = get_mandate(membership, mandate_id)
    return _stop(mandate.id, membership, reason=reason, source="staff_cancel")


def payer_cancel(membership, mandate_id, *, reason: str = "") -> DirectDebitMandate:
    """The payer withdraws their authority. It always works for them, and stops the mandate."""
    mandate = payer_mandate(membership, mandate_id)
    return _stop(mandate.id, membership, reason=reason or "The payer withdrew their authority.", source="payer_cancel")


def suspend(membership, mandate_id) -> DirectDebitMandate:
    """Stop debits for now. Through the provider where it can pause a mandate; otherwise SchoolOS itself will not debit it (the provider's mandate
    stays as it is, and SchoolOS says so)."""
    if not permissions.can_manage(membership):
        raise MandateRefused("Only the owner, or someone the owner has authorised, can manage mandates.", "not_mandate_manager")
    mandate = get_mandate(membership, mandate_id)
    if mandate.status not in (MandateStatus.ACTIVE, MandateStatus.PENDING_PROVIDER_SETUP):
        raise MandateRefused("Only a mandate that is active, or about to be, can be suspended.", "cannot_suspend")
    caps = _connector_of(mandate).info.capabilities
    provider_mandate = _provider_stop(mandate, "suspend_mandate") if caps.supports_suspension else ProviderMandate(
        provider_ref=mandate.provider_mandate_reference, status=MandateStatus.SUSPENDED, provider_status=mandate.provider_status,
    )
    with transaction.atomic():
        mandate = get_locked(mandate.id)
        apply_provider_state(mandate, replace(provider_mandate, status=MandateStatus.SUSPENDED), actor=membership, source="staff_suspend")
    return mandate


def reactivate(membership, mandate_id) -> DirectDebitMandate:
    if not permissions.can_manage(membership):
        raise MandateRefused("Only the owner, or someone the owner has authorised, can manage mandates.", "not_mandate_manager")
    mandate = get_mandate(membership, mandate_id)
    if mandate.status != MandateStatus.SUSPENDED:
        raise MandateRefused("This mandate is not suspended.", "not_suspended")
    caps = _connector_of(mandate).info.capabilities
    if caps.supports_reactivation:
        _provider_stop(mandate, "reactivate_mandate")
    with transaction.atomic():
        mandate = get_locked(mandate.id)
        old = mandate.status
        mandate.status = MandateStatus.ACTIVE if mandate.debit_ready_at else MandateStatus.PENDING_PROVIDER_SETUP
        mandate.suspended_at = None
        mandate.save()
        record_event(mandate, "status_changed", actor=membership, from_status=old, to_status=mandate.status, source="staff_reactivate")
        audit.record(mandate.school, "mandate_reactivated", actor=membership, obj=mandate)
    return refresh_mandate(mandate.id, actor=membership, source="after_reactivate")


def sync_watched(*, limit: int = 200) -> int:
    """Ask the provider about every mandate it still has something to say about (a worker or command calls this). Returns how many changed."""
    changed = 0
    for mandate_id in DirectDebitMandate.objects.filter(status__in=WATCHED).exclude(provider_mandate_reference="").values_list("id", flat=True)[:limit]:
        before = DirectDebitMandate.objects.only("status").get(id=mandate_id).status
        try:
            after = refresh_mandate(mandate_id, source="sync").status
        except Exception:  # noqa: BLE001 - one mandate must not stop the rest
            logger.exception("A mandate could not be refreshed")
            continue
        changed += int(after != before)
    return changed
