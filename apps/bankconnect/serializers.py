"""What the app is allowed to see. Every field is listed by name: a column added to a model later is
not exposed until someone decides it should be. Nothing here can carry a credential."""

from .providers import registry
from .providers.base import Capabilities


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _capabilities(capabilities: Capabilities) -> dict:
    return {_camel(name): value for name, value in capabilities.as_dict().items()}


def _iso(value):
    return value.isoformat() if value else None


def serialize_provider(info) -> dict:
    return {
        "code": info.code,
        "displayName": info.display_name,
        "icon": info.icon,
        "connectionType": info.connection_type,
        "connectMethods": list(info.connect_methods),
        "credentialFields": [
            {"name": f.name, "label": f.label, "secret": f.secret, "required": f.required}
            for f in info.credential_fields
        ],
        "capabilities": _capabilities(info.capabilities),
        "productionStatus": info.production_status,
        "available": info.implemented,
        "isSandbox": info.is_sandbox,
        "description": info.description,
    }


def serialize_connection(connection) -> dict:
    connector = registry.get_connector(connection.provider)
    info = connector.info if connector else None
    return {
        "id": str(connection.id),
        "provider": connection.provider,
        "providerName": info.display_name if info else connection.provider,
        "connectionType": connection.connection_type,
        "isSandbox": connection.is_sandbox,
        "bankName": connection.bank_name,
        "accountName": connection.account_name,
        "accountMask": connection.account_mask,
        "purpose": connection.purpose,
        "label": connection.label,
        "status": connection.status,
        "lastSyncedAt": _iso(connection.last_synced_at),
        "lastErrorCode": connection.last_error_code,
        "tokenExpiresAt": _iso(connection.token_expires_at),
        "webhookConfigured": bool(connection.webhook_token_hash),
        "capabilities": _capabilities(info.capabilities if info else Capabilities()),
        "createdAt": _iso(connection.created_at),
        "disconnectedAt": _iso(connection.disconnected_at),
    }


def serialize_transaction(t) -> dict:
    return {
        "id": str(t.id),
        "connectionId": str(t.connection_id),
        "provider": t.provider,
        "bankName": t.bank_name,
        "bankAccountName": t.bank_account_name,
        "maskedAccountNumber": t.masked_account_number,
        "transactionReference": t.transaction_reference,
        "transactionType": t.transaction_type,
        "direction": t.direction,
        "amountMinor": t.amount_minor,
        "currency": t.currency,
        "senderName": t.sender_name,
        "senderAccountMask": t.sender_account_mask,
        "senderBank": t.sender_bank,
        "narration": t.narration,
        "transactionDate": _iso(t.transaction_date),
        "balanceAfterMinor": t.balance_after_minor,
        "isSandbox": t.is_sandbox,
        "reconciliationStatus": t.reconciliation_status,
        "confidence": t.reconciliation_confidence,
        "matchReasons": t.match_reasons,
        "receivedAt": _iso(t.created_at),
    }


def serialize_audit_event(event) -> dict:
    return {
        "id": str(event.id),
        "kind": event.kind,
        "connectionId": str(event.connection_id) if event.connection_id else None,
        "actorMembershipId": str(event.actor_id) if event.actor_id else None,
        "detail": event.detail,
        "at": _iso(event.at),
    }
