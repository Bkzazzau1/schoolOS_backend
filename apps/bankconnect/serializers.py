"""What the app is allowed to see. Every field is listed by name: a column added to a model later is not exposed until someone decides
it should be. Nothing here can carry a credential."""

from .providers import registry
from .providers.base import CollectionCapabilities


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _capabilities(capabilities: CollectionCapabilities) -> dict:
    return {_camel(name): value for name, value in capabilities.as_dict().items()}


def _iso(value):
    return value.isoformat() if value else None


def serialize_provider(info) -> dict:
    return {
        "code": info.code,
        "displayName": info.display_name,
        "icon": info.icon,
        "environments": list(info.environments),
        "credentialFields": [
            {"name": f.name, "label": f.label, "secret": f.secret, "required": f.required, "help": f.help} for f in info.credential_fields
        ],
        "settingFields": [
            {"name": f.name, "label": f.label, "choices": list(f.choices), "default": f.default, "required": f.required} for f in info.setting_fields
        ],
        "capabilities": _capabilities(info.capabilities),
        "onboarding": info.onboarding,
        "webhook": {
            "mode": info.webhook.mode, "where": info.webhook.where, "verification": info.webhook.verification,
            "events": list(info.webhook.events), "note": info.webhook.note,
        },
        "productionStatus": info.production_status,
        "available": info.implemented,
        "isSandbox": info.is_sandbox,
        "description": info.description,
        "accountLabel": info.account_label,
        "payerNote": info.payer_note,
    }


def serialize_connection(connection) -> dict:
    """A provider connection as the app may see it: which provider, which environment, who the merchant is, whether it is the active
    provider and whether its webhook is known to work. Never a credential, never a settlement account."""
    connector = registry.get_connector(connection.provider)
    info = connector.info if connector else None
    return {
        "id": str(connection.id),
        "provider": connection.provider,
        "providerName": info.display_name if info else connection.provider,
        "environment": connection.environment,
        "isSandbox": connection.is_sandbox,
        "merchantName": connection.merchant_name,
        "merchantReference": connection.merchant_reference,
        "label": connection.label,
        "status": connection.status,
        "isActiveProvider": connection.is_active_provider,
        "webhookStatus": connection.webhook_status,
        "webhookConfirmedAt": _iso(connection.webhook_confirmed_at),
        "lastVerifiedAt": _iso(connection.last_verified_at),
        "lastErrorCode": connection.last_error_code,
        "settings": connection.provider_settings or {},
        "capabilities": _capabilities(info.capabilities if info else CollectionCapabilities()),
        "createdAt": _iso(connection.created_at),
        "disconnectedAt": _iso(connection.disconnected_at),
    }


def serialize_transaction(t) -> dict:
    return {
        "id": str(t.id),
        "connectionId": str(t.connection_id),
        "provider": t.provider,
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
        "provider": t.provider,
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
