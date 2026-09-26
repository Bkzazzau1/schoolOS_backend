"""Pulling a connection's new transactions since its cursor.

Each page is stored and the cursor advanced in one database transaction, so a crash between the two
can only ever repeat a page (which ingestion ignores), never skip one. Two syncs overlapping is
therefore harmless. A failed call marks the connection (see `connections.record_failure`) and keeps
whatever pages already arrived.
"""

from dataclasses import dataclass

from django.db import transaction

from . import audit, connections
from .constants import ConnectionStatus
from .ingestion import CREATED, DUPLICATE, INVALID, ingest
from .providers import registry
from .providers.base import ConnectorError
from .vault import VaultError

PAGE_SIZE = 100
#: One run reads at most this many pages; a very long backlog is finished by the next run.
MAX_PAGES = 20


@dataclass
class SyncOutcome:
    ok: bool = True
    fetched: int = 0
    created: int = 0
    duplicates: int = 0
    invalid: int = 0
    more: bool = False
    error_code: str = ""
    error_message: str = ""


def can_sync(connection) -> bool:
    connector = registry.get_connector(connection.provider)
    return (
        connection.status == ConnectionStatus.CONNECTED
        and connector is not None
        and connector.info.capabilities.supports_transaction_sync
    )


def _fail(connection, outcome: SyncOutcome, code: str, message: str) -> SyncOutcome:
    was = connection.status
    connections.record_failure(connection, code)
    if connection.status != was:
        audit.record(connection.school, "sync_failed", connection=connection, code=code, was=was, now=connection.status)
    outcome.ok, outcome.error_code, outcome.error_message = False, code, message
    return outcome


def sync_connection(connection, *, actor=None, max_pages: int = MAX_PAGES) -> SyncOutcome:
    if connection.status != ConnectionStatus.CONNECTED:
        raise connections.BankRejected("Only a connected account can be synced.", "not_live")
    connector = registry.get_connector(connection.provider)
    if connector is None or not connector.info.capabilities.supports_transaction_sync:
        raise connections.BankRejected("This provider does not report transactions to SchoolOS.", "not_supported")

    outcome = SyncOutcome()
    try:
        connector, secret = connections.open_for_provider(connection)
        cursor = connection.sync_cursor or {}
        for _ in range(max_pages):
            page = connector.fetch_transactions(secret, cursor, PAGE_SIZE)
            with transaction.atomic():
                for item in page.transactions:
                    result = ingest(connection, item)
                    outcome.created += result.outcome == CREATED
                    outcome.duplicates += result.outcome == DUPLICATE
                    outcome.invalid += result.outcome == INVALID
                cursor = page.next_cursor
                connection.sync_cursor = cursor
                connection.save(update_fields=["sync_cursor", "updated_at"])
            outcome.fetched += len(page.transactions)
            if not page.has_more:
                break
        else:
            outcome.more = True
    except ConnectorError as error:
        return _fail(connection, outcome, error.code, error.message)
    except VaultError:
        return _fail(connection, outcome, "vault_error", "The stored credential could not be opened. Reconnect the account.")

    connections.record_success(connection, synced=True)
    if actor is not None:
        audit.record(
            connection.school, "synced", actor=actor, connection=connection,
            fetched=outcome.fetched, created=outcome.created, duplicates=outcome.duplicates, invalid=outcome.invalid,
        )
    return outcome
