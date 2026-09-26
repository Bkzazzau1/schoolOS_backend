"""Putting synthetic money on a sandbox connection, for tests and for trying the feature locally
without a bank. Refuses anything that is not a sandbox connection."""

import json
import uuid
from datetime import datetime, timezone

from . import connections
from .models import SandboxFeedItem
from .providers.sandbox import SIGNATURE_HEADER, SandboxConnector


def _require_sandbox(connection) -> None:
    if not connection.is_sandbox:
        raise ValueError("Only a sandbox connection can be given synthetic transactions.")


def transaction_payload(**over) -> dict:
    """A provider-shaped transaction. Anything can be overridden; the id is fresh unless given."""
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


def add_feed_item(connection, **over) -> SandboxFeedItem:
    """Queue a transaction on the connection's feed, to be picked up by the next sync."""
    _require_sandbox(connection)
    return SandboxFeedItem.objects.create(connection=connection, payload=transaction_payload(**over))


def signed_webhook(connection, **over) -> tuple[bytes, dict]:
    """A body and headers exactly as the sandbox provider would deliver them."""
    _require_sandbox(connection)
    body = json.dumps({"transaction": transaction_payload(**over)}).encode()
    secret = connections.open_secret(connection)
    return body, {SIGNATURE_HEADER: SandboxConnector.sign(body, secret["webhook_secret"])}
