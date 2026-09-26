"""Receiving a provider's callback.

The route is public - a bank cannot sign in - so it has to defend itself:

1. The address carries a long random per-connection token (only its hash is stored); an unknown one
   is a plain 404, so nobody can probe which connections exist.
2. The provider's own signature is verified BEFORE the body is trusted or anything is stored.
3. The delivery is recorded by a hash of the body and the connection, so the same delivery arriving
   twice is recognised and not processed twice.
4. The record of the delivery and the transactions it carried are one database transaction, so a
   delivery that failed half-way is retried by the provider instead of being remembered as done.
"""

import hashlib
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import audit, connections, identifiers
from .constants import ConnectionStatus
from .ingestion import CREATED, ingest
from .models import BankConnection, BankWebhookEvent
from .providers import registry
from .providers.base import ConnectorError, InvalidSignature
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


def _connection_for(provider: str, token: str) -> BankConnection:
    connection = (
        BankConnection.objects.select_related("school")
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
        # Not being read right now. Nothing is stored; a sync picks the money up on enabling.
        return WebhookResult("ignored")

    try:
        secret = connections.open_secret(connection)
        items = registry.get_connector(provider).handle_webhook(raw_body=raw_body, headers=headers, secret=secret)
    except InvalidSignature:
        audit.record(connection.school, "webhook_rejected", connection=connection, code="invalid_signature")
        raise WebhookRefused(400, "invalid_signature")
    except ConnectorError:
        raise WebhookRefused(400, "unreadable")
    except VaultError:
        raise WebhookRefused(503, "unavailable")  # the provider retries later

    delivery = hashlib.sha256(str(connection.id).encode() + b":" + raw_body).hexdigest()
    created = 0
    with transaction.atomic():
        try:
            with transaction.atomic():
                event = BankWebhookEvent.objects.create(
                    provider=provider, connection=connection, payload_hash=delivery, signature_valid=True
                )
        except IntegrityError:
            return WebhookResult("duplicate")
        for item in items:
            created += ingest(connection, item).outcome == CREATED
        event.outcome, event.processed_at = "processed", timezone.now()
        event.save(update_fields=["outcome", "processed_at"])
    return WebhookResult("processed", created)
