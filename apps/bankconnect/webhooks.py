"""Receiving a provider's callback.

The route is public - a provider cannot sign in - so it has to defend itself:

1. The address carries a long random per-connection token (only its hash is stored); an unknown one is a plain 404, so nobody can
   probe which connections exist.
2. The provider's own authenticity check is applied BEFORE the body is trusted or anything is stored: its signature where it signs
   (Paystack; Monnify live), or, where it does not sign (Monnify sandbox), a direct question to the provider whose answer -
   not the body - decides what was paid and into which account. A forged body is refused; nothing is ever taken on its word.
3. The delivery is recorded by a hash of the body and the connection, so the same delivery arriving twice is recognised and not
   processed twice - and not asked about twice.
4. The record of the delivery and the payments it carried are one database transaction, so a delivery that failed half-way is retried
   by the provider instead of being remembered as done. No provider is called while a database transaction is open.

Only once a verified event has arrived is the connection's webhook called active.
"""

import hashlib
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import audit, identifiers, provider_connections, reconciliation
from .constants import ConnectionStatus
from .ingestion import CREATED, ingest
from .models import BankWebhookEvent, CollectionProviderConnection
from .providers import registry
from .providers.base import ConnectorError, InvalidSignature, NotSupported, ProviderUnavailable
from .vault import VaultError

MAX_BODY_BYTES = 64 * 1024


class WebhookRefused(Exception):
    """Turned into an HTTP status by the view. The message never echoes the request."""

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class WebhookResult:
    outcome: str  # processed | duplicate | ignored
    created: int = 0


def _connection_for(provider: str, token: str) -> CollectionProviderConnection:
    connection = (
        CollectionProviderConnection.objects.select_related("school")
        .filter(provider=provider, webhook_token_hash=identifiers.hash_token(token))
        .exclude(status=ConnectionStatus.REVOKED)
        .first()
    )
    connector = registry.get_connector(provider)
    if connection is None or connector is None or not connector.info.capabilities.supports_webhooks:
        raise WebhookRefused(404, "not_found")
    return connection


def receive(provider: str, token: str, raw_body: bytes, headers: dict) -> WebhookResult:
    if len(raw_body) > MAX_BODY_BYTES:
        raise WebhookRefused(413, "too_large")
    connection = _connection_for(provider, token)
    if connection.status in (ConnectionStatus.PENDING, ConnectionStatus.DISABLED):
        return WebhookResult("ignored")  # not in use right now: nothing is stored

    delivery = hashlib.sha256(str(connection.id).encode() + b":" + raw_body).hexdigest()
    if BankWebhookEvent.objects.filter(provider=provider, payload_hash=delivery).exists():
        return WebhookResult("duplicate")  # a delivery already verified and recorded: not checked or processed again

    try:
        secret = provider_connections.open_secret(connection)
        outcome = registry.get_connector(provider).handle_webhook(
            raw_body=raw_body, headers=headers, secret=secret, environment=connection.environment,
            settings=dict(connection.provider_settings or {}),
        )
    except InvalidSignature:
        # Unauthenticated: this body is not shown to be the provider's. Logged for visibility, but never
        # treated as evidence the connection's own credential is bad - that would let noise or an attacker
        # put a real connection into "needs attention".
        audit.record(connection.school, "webhook_rejected", connection=connection, code="invalid_signature")
        raise WebhookRefused(400, "invalid_signature")
    except NotSupported:
        raise WebhookRefused(404, "not_found")
    except ProviderUnavailable as exc:
        # Authenticated so far as we could tell, but we could not finish checking it; the provider retries.
        _record_processing_failure(connection, exc.code)
        raise WebhookRefused(503, "unavailable")
    except ConnectorError as exc:
        _record_processing_failure(connection, exc.code)
        raise WebhookRefused(400, "unreadable")
    except VaultError:
        # Our own secret storage failed to open - not the provider's fault, but still something a person
        # must see: this school's connection cannot be read right now.
        _record_processing_failure(connection, "vault_error")
        raise WebhookRefused(503, "unavailable")

    created = 0
    try:
        with transaction.atomic():
            try:
                with transaction.atomic():
                    event = BankWebhookEvent.objects.create(provider=provider, connection=connection, payload_hash=delivery, signature_valid=True)
            except IntegrityError:
                return WebhookResult("duplicate")
            for item in outcome.transactions:
                created += ingest(connection, item).outcome == CREATED
            event.outcome = "processed" if (outcome.transactions or outcome.account_events) else "ignored"
            event.processed_at = timezone.now()
            event.save(update_fields=["outcome", "processed_at"])
            if outcome.account_events:
                _account_events(connection, outcome.account_events)
    except Exception:
        # The whole attempt rolled back together, including the event row above: nothing here was half
        # recorded, so a retry of the same delivery starts fresh rather than being mistaken for a duplicate.
        _record_processing_failure(connection, "processing_failed")
        raise WebhookRefused(503, "unavailable")
    provider_connections.record_success(connection)  # clears any earlier failure: the connection is working again
    provider_connections.confirm_webhook(connection)
    if created:
        reconciliation.reconcile_quietly(connection.school)
    return WebhookResult("processed" if (outcome.transactions or outcome.account_events) else "ignored", created)


def _record_processing_failure(connection: CollectionProviderConnection, code: str) -> None:
    """An authenticated (or not-yet-disproven) event that failed somewhere after that: unlike a rejected
    signature, this is real evidence the connection needs a look, so it is the same signal a failed
    connectivity test gives - the connection's own status - plus an audit entry for the detail. Never
    passed anything from the provider's own body or our stored credential: `code` is always one of the
    app's own short, fixed words."""
    provider_connections.record_failure(connection, code)
    audit.record(connection.school, "webhook_processing_failed", connection=connection, code=code)


def _account_events(connection, events) -> None:
    """The provider says an account it was asked to make is ready, or could not be made. Handled by the family collection accounts."""
    from apps.receivables import collection_accounts

    for event in events:
        collection_accounts.on_provider_account_event(connection, event)
