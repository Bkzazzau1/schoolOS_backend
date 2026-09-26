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
        "duplicateOf": str(t.duplicate_of_id) if t.duplicate_of_id else None,
        "familyId": str(t.family_id) if t.family_id else None,
        "receivingAccountRef": t.receiving_account_ref,
        "allocations": [serialize_allocation(a) for a in _active_allocations(t)],
        "receivedAt": _iso(t.created_at),
    }


def serialize_transaction_brief(t) -> dict:
    """Enough for a dashboard's recent-payments list, without the matching detail."""
    return {
        "id": str(t.id),
        "senderName": t.sender_name,
        "amountMinor": t.amount_minor,
        "currency": t.currency,
        "transactionDate": _iso(t.transaction_date),
        "bankName": t.bank_name,
        "maskedAccountNumber": t.masked_account_number,
        "reconciliationStatus": t.reconciliation_status,
        "isSandbox": t.is_sandbox,
    }


def _active_allocations(t):
    prefetched = getattr(t, "active_allocations", None)
    if prefetched is not None:
        return prefetched
    return t.allocations.filter(superseded=False).select_related("student", "receivable")


def serialize_allocation(a) -> dict:
    return {
        "id": str(a.id),
        "studentId": str(a.student_id),
        "studentName": a.student.full_name,
        "studentCode": a.student.student_code,
        "purpose": a.purpose,
        "amountMinor": a.amount_minor,
        "source": a.source,
        "superseded": a.superseded,
        "familyId": str(a.family_id) if a.family_id else None,
        "receivableId": str(a.receivable_id) if a.receivable_id else None,
        "itemName": a.receivable.item_name if a.receivable_id else "",
    }


def serialize_decision(d) -> dict:
    user = d.actor.user if d.actor_id else None
    return {
        "id": str(d.id),
        "action": d.action,
        "note": d.note,
        "actorMembershipId": str(d.actor_id) if d.actor_id else None,
        "actorRole": d.actor.role if d.actor_id else None,
        "actorName": (user.get_full_name() or user.email) if user else "SchoolOS matching",
        "before": d.before,
        "after": d.after,
        "at": _iso(d.at),
    }


def serialize_transaction_detail(t) -> dict:
    """The payment with every allocation it has ever had and every decision made about it."""
    body = serialize_transaction(t)
    from apps.receivables import credit

    body["allocations"] = [serialize_allocation(a) for a in t.allocations.select_related("student", "receivable")]
    #: Money from this payment held as family credit because the family owed less than was paid.
    body["familyCreditMinor"] = credit.total_credit_from(t)
    body["decisions"] = [serialize_decision(d) for d in t.decisions.select_related("actor__user")]
    return body


def serialize_audit_event(event) -> dict:
    return {
        "id": str(event.id),
        "kind": event.kind,
        "connectionId": str(event.connection_id) if event.connection_id else None,
        "actorMembershipId": str(event.actor_id) if event.actor_id else None,
        "detail": event.detail,
        "at": _iso(event.at),
    }
