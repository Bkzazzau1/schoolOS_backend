"""Mandates & Direct Debit as the app may see it. Every serializer here is a whitelist: a credential, a sealed value, a full account number and a
provider's raw answer are simply never among the fields."""

from django.conf import settings

from . import consent, mandates
from .constants import LIVE_MANDATE, ConnectionStatus, MandateStatus
from .providers import registry
from .providers.base import MandateCapabilities, MandateProviderInfo


def _iso(value):
    return value.isoformat() if value else None


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _capabilities(capabilities: MandateCapabilities) -> dict:
    return {_camel(name): value for name, value in capabilities.as_dict().items()}


def live_available(info: MandateProviderInfo) -> dict:
    """Whether LIVE use is switched on for this provider on this server, and if not why - never a claim that a school is eligible."""
    if info.code == "lendsqr":
        on = bool(getattr(settings, "MANDATES_LENDSQR_LIVE_ENABLED", False))
        return {"live": on, "why": "" if on else "Live Lendsqr mandates are not switched on for this server. Lendsqr needs to confirm that your organisation is licensed as a lender or otherwise entitled to use it."}
    if info.code == "remita":
        on = bool(getattr(settings, "MANDATES_REMITA_LIVE_BASE_URL", ""))
        return {"live": on, "why": "" if on else "Remita's live address has not been configured on this server yet, and Remita requires a UAT before go-live."}
    return {"live": False, "why": "Test only."}


def provider(info: MandateProviderInfo) -> dict:
    return {
        "code": info.code, "displayName": info.display_name, "icon": info.icon, "environments": list(info.environments),
        "credentialFields": [{"name": f.name, "label": f.label, "secret": f.secret, "required": f.required, "help": f.help} for f in info.credential_fields],
        "capabilities": _capabilities(info.capabilities), "onboarding": info.onboarding, "description": info.description,
        "webhook": {"mode": info.webhook.mode, "where": info.webhook.where, "verification": info.webhook.verification, "events": list(info.webhook.events), "note": info.webhook.note},
        "productionStatus": info.production_status, "available": info.implemented, "isSandbox": info.is_sandbox, "liveNote": info.live_note,
        "liveAvailability": live_available(info), "activationNote": info.activation_note, "payerRequirements": list(info.payer_requirements),
        "maximumNote": info.maximum_note, "maximumScope": info.maximum_scope,
    }


def connection(c, *, counts: dict | None = None) -> dict:
    connector = registry.get_connector(c.provider)
    info = connector.info if connector else None
    return {
        "id": str(c.id), "provider": c.provider, "providerName": info.display_name if info else c.provider, "environment": c.environment,
        "isSandbox": c.is_sandbox, "merchantName": c.merchant_name, "merchantReference": c.merchant_reference, "label": c.label, "status": c.status,
        "webhookStatus": c.webhook_status, "webhookConfirmedAt": _iso(c.webhook_confirmed_at), "lastVerifiedAt": _iso(c.last_verified_at),
        "lastErrorCode": c.last_error_code, "capabilities": _capabilities(info.capabilities) if info else {}, "mandateCounts": counts or {},
        "createdAt": _iso(c.created_at), "disconnectedAt": _iso(c.disconnected_at), "usable": c.status == ConnectionStatus.CONNECTED,
    }


def _consent(m) -> dict | None:
    if not m.consent_at:
        return None
    return {"at": _iso(m.consent_at), "channel": m.consent_channel, "version": m.consent_version, "reference": m.consent_reference}


def mandate(m, *, detail: bool = False, staff: bool = True) -> dict:
    """A mandate. Never carries an account number: a bank, a masked number and nothing else about the account."""
    ready, why = mandates.is_debit_ready(m)
    connector = registry.get_connector(m.provider)
    info = connector.info if connector else None
    out = {
        "id": str(m.id), "familyId": str(m.family_id), "familyName": m.family.display_name, "familyCode": m.family.code,
        "payer": {"id": str(m.payer_id), "name": m.payer.guardian.name, "relationship": m.payer.guardian.relationship, "hasAppAccount": bool(m.payer.guardian.account_user_id)},
        "provider": m.provider, "providerName": info.display_name if info else m.provider, "connectionId": str(m.provider_connection_id),
        "environment": m.environment, "isSandbox": m.provider_connection.is_sandbox, "bankCode": m.bank_code, "bankName": m.bank_name,
        "accountMask": m.account_mask, "status": m.status, "providerStatus": m.provider_status, "consentRoute": m.consent_route, "consent": _consent(m),
        "maximumAmountMinor": m.maximum_amount_minor, "maximumNote": info.maximum_note if info else "", "maxDebits": m.max_debits,
        "startDate": _iso(m.start_date), "endDate": _iso(m.end_date), "isPrimary": m.is_primary, "debitReady": ready, "debitReadyReason": why,
        "activationStartedAt": _iso(m.activation_started_at), "activatedAt": _iso(m.activated_at), "debitReadyAt": _iso(m.debit_ready_at),
        "activationDeadline": _iso(m.activation_deadline), "suspendedAt": _iso(m.suspended_at), "cancelledAt": _iso(m.cancelled_at),
        "expiredAt": _iso(m.expired_at), "failedAt": _iso(m.failed_at), "failureCode": m.failure_code, "createdAt": _iso(m.created_at),
        "isLive": m.status in LIVE_MANDATE, "lastSyncedAt": _iso(m.last_synced_at),
    }
    if staff:
        out["providerCustomerRef"] = m.provider_customer_ref
        out["mandateCode"] = m.mandate_code
    if detail:
        out["activation"] = activation(m)
        out["events"] = [
            {"kind": e.kind, "from": e.from_status, "to": e.to_status, "at": _iso(e.at), "actor": e.actor.user.get_full_name() if e.actor_id and e.actor.user_id else ""}
            for e in m.events.select_related("actor__user").order_by("id")[:100]
        ]
    return out


def activation(m) -> dict:
    """How the payer activates it, in words and facts they may see. Never a secret: the payer's own account number is not among them."""
    details = dict(m.activation_details or {})
    connector = registry.get_connector(m.provider)
    caps = connector.info.capabilities if connector else MandateCapabilities()
    return {
        "note": connector.info.activation_note if connector else "", "deadline": _iso(m.activation_deadline),
        "canActivateInApp": bool(caps.supports_otp_activation and m.status == MandateStatus.PENDING_ACTIVATION),
        "formUrl": str(details.get("form") or ""), "method": str(details.get("method") or ""),
        "transfer": {
            key: details.get(key) for key in ("amountMinor", "toBank", "toAccount", "windowHours") if details.get(key) not in (None, "")
        } if details.get("method") == "transfer" else {},
        "challengeFields": list(details.get("challengeFields") or []),
    }


def payer_view(m) -> dict:
    """What the payer sees of their own mandate: the school, the bank, the masked account, the limit, how it is activated and what they are agreeing to."""
    base = mandate(m, detail=False, staff=False)
    text = consent.consent_text(m)
    base.update({
        "schoolName": m.school.name, "activation": activation(m), "consentRequired": m.status == MandateStatus.PENDING_CONSENT,
        "consentText": text, "consentVersion": consent.CONSENT_VERSION, "consentTextHash": consent.text_hash(text),
    })
    base.pop("payer", None)
    return base


def batch(b) -> dict:
    return {
        "id": str(b.id), "title": b.title, "status": b.status, "version": b.version, "snapshotHash": b.snapshot_hash,
        "approvedSnapshotHash": b.approved_snapshot_hash, "session": {"id": str(b.session_id), "name": b.session.name},
        "term": {"id": str(b.term_id), "name": b.term.name} if b.term_id else None, "familyCount": b.family_count, "totalItems": b.total_items,
        "totalOutstandingMinor": b.total_outstanding_minor, "totalAmountMinor": b.total_amount_minor, "successCount": b.success_count,
        "failedCount": b.failed_count, "preparedBy": _who(b.prepared_by), "preparedAt": _iso(b.prepared_at), "submittedBy": _who(b.submitted_by),
        "submittedAt": _iso(b.submitted_at), "approvedBy": _who(b.approved_by), "approvedAt": _iso(b.approved_at), "rejectedBy": _who(b.rejected_by),
        "rejectedAt": _iso(b.rejected_at), "rejectionReason": b.rejection_reason, "startedAt": _iso(b.started_at), "completedAt": _iso(b.completed_at),
        "previewBuiltAt": _iso(b.preview_built_at),
    }


def _who(membership):
    if membership is None:
        return None
    user = membership.user
    return {"id": str(membership.id), "name": user.get_full_name() or user.email}


def item(i) -> dict:
    return {
        "id": str(i.id), "familyId": str(i.family_id), "familyName": i.family.display_name, "familyCode": i.family.code,
        "payer": i.payer.guardian.name if i.payer_id else "", "provider": i.provider, "mandateId": str(i.mandate_id) if i.mandate_id else None,
        "bankName": i.mandate.bank_name if i.mandate_id else "", "accountMask": i.mandate.account_mask if i.mandate_id else "", "mandateStatus": i.mandate_status,
        "selected": i.selected, "outstandingMinor": i.outstanding_minor, "eligibleMinor": i.eligible_minor, "maximumMinor": i.maximum_minor,
        "proposedDebitMinor": i.proposed_debit_minor, "amountAdjusted": i.amount_adjusted, "allocation": i.receivable_allocation,
        "eligibilityStatus": i.eligibility_status, "eligibilityNote": i.eligibility_note, "status": i.status, "attemptCount": i.attempt_count,
        "providerStatus": i.provider_status, "errorCode": i.error_code, "errorMessage": i.safe_error_message, "lastAttemptAt": _iso(i.last_attempt_at),
        "completedAt": _iso(i.completed_at), "canSelect": i.eligibility_status == "eligible",
    }


def transaction_row(t) -> dict:
    return {
        "id": str(t.id), "familyId": str(t.family_id), "familyName": t.family.display_name, "mandateId": str(t.mandate_id), "provider": t.provider,
        "amountMinor": t.amount_minor, "currency": t.currency, "status": t.status, "providerStatus": t.provider_status, "failureCode": t.failure_code,
        "confirmedAt": _iso(t.confirmed_at), "reversedAt": _iso(t.reversed_at), "refundedAt": _iso(t.refunded_at), "settled": t.payment_id is not None,
        "createdAt": _iso(t.created_at), "isSandbox": t.provider_connection.is_sandbox, "accountMask": t.mandate.account_mask, "bankName": t.mandate.bank_name,
    }


def audit_event(e) -> dict:
    return {"id": e.id, "kind": e.kind, "objectType": e.object_type, "objectId": e.object_id, "detail": e.detail, "at": _iso(e.at),
            "actor": e.actor.user.get_full_name() if e.actor_id and e.actor.user_id else ""}
