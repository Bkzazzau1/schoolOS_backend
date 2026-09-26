"""Receiving a provider's callback for a mandate or a debit.

The route is public - a provider cannot sign in - so it has to defend itself:

1. The address carries a long random per-connection token (only its hash is stored); an unknown one is a plain 404, so nobody can probe which
   connections exist. A provider that SchoolOS does not accept callbacks from (Lendsqr documents none) is a 404 too.
2. Each provider's own authenticity rule is applied BEFORE anything is believed. Remita documents no signature on its notifications, so a body is never
   trusted: it says only WHICH mandate or debit to ask about, and SchoolOS asks Remita - whose answer, not the body, decides. A forged body about a
   mandate or debit that does not exist changes nothing; one about a real one only makes SchoolOS ask.
3. The delivery is recorded by a hash of the body and the connection, so the same delivery arriving twice is recognised and not processed twice - and
   not asked about twice. It is recorded only AFTER it was processed, so a delivery that failed half-way (the provider could not be reached) is retried by
   the provider instead of being remembered as done.
4. Nothing here can put money in a ledger by itself: only the confirmed status a provider returns to a debit query settles anything (see settlement.py).
"""

import hashlib
from dataclasses import dataclass

from django.utils import timezone

from . import audit, connections, execution, identifiers, mandate_provider
from .constants import ConnectionStatus
from .models import DirectDebitMandate, MandateDebitInstruction, MandateProviderConnection, MandateProviderEvent
from .outcomes import RETRY
from .providers import registry
from .providers.base import ConnectorError, InvalidSignature, NotSupported, ProviderUnavailable
from .vault import VaultError

MAX_BODY_BYTES = 256 * 1024


class WebhookRefused(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


@dataclass
class WebhookResult:
    outcome: str  # processed | duplicate | ignored
    matched: int = 0


def _connection_for(provider: str, token: str) -> MandateProviderConnection:
    connection = (
        MandateProviderConnection.objects.select_related("school").filter(provider=provider, webhook_token_hash=identifiers.hash_token(token))
        .exclude(status=ConnectionStatus.REVOKED).first()
    )
    connector = registry.get_connector(provider)
    if connection is None or connector is None or not connector.info.capabilities.supports_webhooks:
        raise WebhookRefused(404, "not_found")
    return connection


def _process(connection, event) -> bool:
    """Ask the provider about what the callback names. Returns whether it named something SchoolOS knows."""
    if event.kind == "mandate":
        mandate = DirectDebitMandate.objects.filter(provider_connection=connection, provider_mandate_reference=event.mandate_ref).first()
        if mandate is None:
            return False
        mandate_provider.refresh_mandate(mandate.id, source="callback", strict=True)
        return True
    item = MandateDebitInstruction.objects.filter(mandate__provider_connection=connection, request_ref=event.request_ref).first()
    if item is None:
        return False
    outcome = execution.requery_instruction(item.id, force=True)
    if outcome.result == RETRY:
        raise ProviderUnavailable()
    return True


def receive(provider: str, token: str, raw_body: bytes, headers: dict) -> WebhookResult:
    if len(raw_body) > MAX_BODY_BYTES:
        raise WebhookRefused(413, "too_large")
    connection = _connection_for(provider, token)
    if connection.status in (ConnectionStatus.PENDING, ConnectionStatus.DISABLED):
        return WebhookResult("ignored")
    delivery = hashlib.sha256(str(connection.id).encode() + b":" + raw_body).hexdigest()
    if MandateProviderEvent.objects.filter(provider=provider, payload_hash=delivery).exists():
        return WebhookResult("duplicate")
    try:
        secret = connections.open_secret(connection)
        outcome = registry.get_connector(provider).handle_webhook(
            raw_body=raw_body, headers=headers, secret=secret, environment=connection.environment, settings=dict(connection.provider_settings or {}),
        )
    except InvalidSignature:
        audit.record(connection.school, "webhook_rejected", obj=connection, code="invalid_signature")
        raise WebhookRefused(400, "invalid_signature")
    except NotSupported:
        raise WebhookRefused(404, "not_found")
    except ConnectorError:
        raise WebhookRefused(400, "unreadable")
    except VaultError:
        raise WebhookRefused(503, "unavailable")
    matched = 0
    if not outcome.ignored:
        try:
            matched = sum(1 for event in outcome.events if _process(connection, event))
        except (ProviderUnavailable, ConnectorError):
            raise WebhookRefused(503, "unavailable")  # the provider is asked to try again; nothing is remembered as done
    MandateProviderEvent.objects.get_or_create(
        provider=provider, payload_hash=delivery,
        defaults={"connection": connection, "authenticity_valid": matched > 0, "event_type": (outcome.events[0].kind if outcome.events else "ignored")[:24],
                  "outcome": "processed" if matched else "ignored", "processed_at": timezone.now()},
    )
    if matched:
        connections.confirm_webhook(connection)  # only now, with an event that named something real and that the provider itself confirmed
    return WebhookResult("processed" if matched else "ignored", matched)
