"""What the API is allowed to say. Every field is named: a column added to a model later is not exposed
until someone decides it should be, and nothing here can carry a provider credential or internal detail.

Money is always whole minor units (`...Minor`, kobo) - the canonical value - together with the currency;
nothing here does any arithmetic on it.
"""

from . import families, ledger


def _iso(value):
    return value.isoformat() if value else None


def _time(value):
    return value.isoformat() if value else None


def position(pos: ledger.FamilyPosition) -> dict:
    return {
        "grossMinor": pos.gross, "adjustmentsMinor": pos.adjustments, "netMinor": pos.net, "paidMinor": pos.paid,
        "outstandingMinor": pos.outstanding, "overdueMinor": pos.overdue, "arrearsMinor": pos.arrears,
        "currentMinor": pos.current, "creditMinor": pos.credit, "collectibleMinor": pos.collectible,
        "charges": pos.charges, "currency": "NGN",
    }


def student_brief(student) -> dict:
    return {"id": str(student.id), "name": student.full_name, "studentCode": student.student_code, "status": student.status}


def guardian(link) -> dict:
    return {"id": str(link.guardian_id), "name": link.guardian.name, "relationship": link.guardian.relationship, "phone": link.guardian.phone, "isPrimaryPayer": link.is_primary_payer, "active": link.is_active}


def account(a) -> dict:
    """A collection account as a client may see it: never `provider_meta`, never a credential."""
    return {
        "id": str(a.id), "familyId": str(a.family_id), "provider": a.provider, "bankName": a.bank_name,
        "accountNumber": a.account_number, "accountName": a.account_name, "currency": a.currency, "status": a.status,
        "activatedAt": _time(a.activated_at), "dormantAt": _time(a.dormant_at), "statusChangedAt": _time(a.status_changed_at),
    }


def family(f, *, detail: bool = False) -> dict:
    body = {"id": str(f.id), "code": f.code, "displayName": f.display_name, "status": f.status, "createdAt": _time(f.created_at)}
    if detail:
        body["students"] = [student_brief(s) for s in families.active_students(f)]
        body["guardians"] = [guardian(g) for g in f.guardians.select_related("guardian")]
        body["position"] = position(ledger.family_position(f))
        body["collectionAccounts"] = [account(a) for a in f.collection_accounts.exclude(status="closed")]
    return body


def receivable(r, pos: ledger.Position) -> dict:
    return {
        "id": str(r.id), "studentId": str(r.student_id), "studentName": r.student.full_name, "familyId": str(r.family_id),
        "scheduleId": str(r.schedule_id), "feeItemId": str(r.fee_item_id), "itemCode": r.item_code, "itemName": r.item_name,
        "category": r.item_category, "sessionId": str(r.session_id), "sessionName": r.session.name,
        "termId": str(r.term_id) if r.term_id else None, "termName": r.term.name if r.term_id else "",
        "currency": r.currency, "grossMinor": pos.gross, "adjustmentsMinor": pos.adjustments, "netMinor": pos.net,
        "paidMinor": pos.paid, "outstandingMinor": pos.outstanding, "dueDate": r.due_date.isoformat(), "status": r.status,
        "installment": r.installment_number, "installments": r.installment_count, "chargeKey": str(r.charge_key),
        "voidedAt": _time(r.voided_at), "voidReason": r.void_reason, "createdAt": _time(r.created_at),
    }


def adjustment(a) -> dict:
    return {
        "id": str(a.id), "receivableId": str(a.receivable_id), "kind": a.kind, "amountMinor": a.amount_minor, "reason": a.reason,
        "isReversal": a.reverses_id is not None, "reverses": str(a.reverses_id) if a.reverses_id else None,
        "groupId": str(a.group_id) if a.group_id else None,
        "requestedBy": str(a.requested_by_id) if a.requested_by_id else None,
        "authorizedBy": str(a.authorized_by_id) if a.authorized_by_id else None, "authorizedAt": _time(a.authorized_at),
        "source": a.source_ref, "createdAt": _time(a.created_at),
    }


def credit_entry(e) -> dict:
    return {
        "id": str(e.id), "kind": e.kind, "amountMinor": e.amount_minor, "currency": e.currency, "reason": e.reason,
        "transactionId": str(e.transaction_id) if e.transaction_id else None,
        "receivableId": str(e.receivable_id) if e.receivable_id else None, "reverses": str(e.reverses_id) if e.reverses_id else None,
        "createdAt": _time(e.created_at),
    }


def item(i) -> dict:
    return {
        "id": str(i.id), "code": i.code, "name": i.name, "category": i.category, "amountMinor": i.amount_minor, "currency": i.currency,
        "isMandatory": i.is_mandatory, "dueDate": _iso(i.due_date), "plan": i.plan, "sortOrder": i.sort_order, "scope": i.scope,
        "section": i.section, "academicClassId": str(i.academic_class_id) if i.academic_class_id else None,
        "studentId": str(i.student_id) if i.student_id else None,
    }


def schedule(s, *, items: bool = False) -> dict:
    body = {
        "id": str(s.id), "name": s.name, "status": s.status, "sessionId": str(s.session_id), "termId": str(s.term_id) if s.term_id else None,
        "replaces": str(s.replaces_id) if s.replaces_id else None, "publishedAt": _time(s.published_at), "retiredAt": _time(s.retired_at),
        "retireReason": s.retire_reason, "createdAt": _time(s.created_at),
    }
    if items:
        body["items"] = [item(i) for i in s.items.all()]
    return body


def publish_report(report) -> dict:
    return {
        "created": report.created, "alreadyExisted": report.already_existed, "families": len(report.families),
        "withoutFamily": [student_brief(s) for s in report.without_family],
        "unclassified": [student_brief(s) for s in report.unclassified],
        "joinedLater": [student_brief(s) for s in report.joined_later],
        "alreadyChargedByReplaced": report.already_charged_by_replaced, "complete": report.complete,
    }


def statement_record(s) -> dict:
    return {
        "id": str(s.id), "number": s.number, "status": s.status, "familyId": str(s.family_id), "sessionId": str(s.session_id),
        "termId": str(s.term_id) if s.term_id else None, "issuedAt": _time(s.issued_at), "issuedBy": str(s.issued_by_id) if s.issued_by_id else None,
        "snapshot": s.snapshot,
    }


def allocation_row(a) -> dict:
    return {
        "id": str(a.id), "transactionId": str(a.transaction_id), "studentId": str(a.student_id), "receivableId": str(a.receivable_id) if a.receivable_id else None,
        "purpose": a.purpose, "amountMinor": a.amount_minor, "source": a.source, "superseded": a.superseded, "createdAt": _time(a.created_at),
    }
