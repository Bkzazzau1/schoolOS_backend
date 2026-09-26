"""Putting synthetic payments through the sandbox provider, for tests and for trying the feature locally without a real provider.
Refuses anything that is not a sandbox connection. A delivered payment goes through exactly the path a real provider's webhook does:
the public route, the signature check, deduplication, ingestion and reconciliation."""

import json
import uuid
from datetime import datetime, timezone

from . import provider_connections, webhooks
from .models import SandboxFeedItem
from .providers.sandbox import SIGNATURE_HEADER, SandboxConnector


def _require_sandbox(connection) -> None:
    if not connection.is_sandbox:
        raise ValueError("Only a sandbox connection can be given synthetic payments.")


def transaction_payload(**over) -> dict:
    """A provider-shaped payment. Anything can be overridden; the id is fresh unless given."""
    payload = {
        "external_transaction_id": f"SBX-{uuid.uuid4().hex[:12]}",
        "direction": "credit",
        "amount_minor": 100_000,
        "transaction_date": datetime.now(timezone.utc).isoformat(),
        "sender_name": "",
        "narration": "",
    }
    payload.update(over)
    return payload


def record_payment(connection, **over) -> dict:
    """The provider's own record of a payment (what `verify_transaction` asks about)."""
    _require_sandbox(connection)
    payload = transaction_payload(**over)
    SandboxFeedItem.objects.create(connection=connection, payload=payload)
    return payload


def signed_webhook(connection, **over) -> tuple[bytes, dict]:
    """A body and headers exactly as the sandbox provider would deliver them."""
    payload = record_payment(connection, **over)
    body = json.dumps({"transaction": payload}).encode()
    secret = provider_connections.open_secret(connection)
    return body, {SIGNATURE_HEADER: SandboxConnector.sign(body, secret["webhook_secret"])}


def signed_account_event(connection, **event) -> tuple[bytes, dict]:
    _require_sandbox(connection)
    body = json.dumps({"account_event": event}).encode()
    secret = provider_connections.open_secret(connection)
    return body, {SIGNATURE_HEADER: SandboxConnector.sign(body, secret["webhook_secret"])}


def deliver(connection, **over):
    """Send one synthetic payment through the real webhook path and return its result."""
    body, headers = signed_webhook(connection, **over)
    token = provider_connections.open_secret(connection)[provider_connections.WEBHOOK_KEY]
    return webhooks.receive(connection.provider, token, body, headers)
