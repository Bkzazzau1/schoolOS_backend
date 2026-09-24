from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import boolean, choice, text
from apps.sync.models import SyncRecord
from apps.sync.registry import EntityHandler, MutationContext

from .models import (
    AcademicLifecycleStatus,
    ProgressionBatchStatus,
    ProgressionOutcome,
)
from .services import (
    serialize_progression_batch,
    upsert_academic_class,
    upsert_academic_session,
    upsert_academic_term,
    upsert_progression_batch,
)

SESSION_ENTITY = "academic_session"
TERM_ENTITY = "academic_term"
CLASS_ENTITY = "academic_class"
PROGRESSION_BATCH_ENTITY = "academic_progression_batch"

_ADMIN_ROLES = frozenset({"administrator"})
_READ_ROLES = {"administrator", "proprietor", "principal"}
_SESSION_STATUSES = set(AcademicLifecycleStatus.values)
_BATCH_STATUSES = set(ProgressionBatchStatus.values)
_OUTCOMES = set(ProgressionOutcome.values)


def _optional_text(payload: dict, key: str, *, max_len: int = 250) -> str:
    return text(payload, key, max_len=max_len, required=False)


def _positive_int(payload: dict, key: str, *, maximum: int = 10000) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise Rejected(f"{key} must be an integer.")
    if value < 1 or value > maximum:
        raise Rejected(f"{key} is outside the allowed range.")
    return value


def _freeze_closed(existing: dict | None, cleaned: dict, keys: tuple[str, ...], label: str):
    if existing is None or existing.get("status") != "closed":
        return
    if cleaned.get("status") != "closed":
        raise Rejected(f"A closed {label} cannot be reopened.")
    if any(existing.get(key) != cleaned.get(key) for key in keys):
        raise Rejected(f"A closed {label} is historical and cannot be rewritten.")


class AcademicSessionHandler(EntityHandler):
    entity_type = SESSION_ENTITY
    roles = _ADMIN_ROLES

    def visible(self, membership, payload):
        return payload if membership.role in _READ_ROLES else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        external_id = text(p, "id", max_len=64)
        if external_id != ctx.entity_id:
            raise Rejected("id must match the academic-session entity id.")
        cleaned = {
            "id": external_id,
            "code": text(p, "code", max_len=40),
            "name": text(p, "name", max_len=120),
            "startsOn": text(p, "startsOn", max_len=10),
            "endsOn": text(p, "endsOn", max_len=10),
            "status": choice(p.get("status"), _SESSION_STATUSES, "status"),
        }
        _freeze_closed(
            ctx.existing,
            cleaned,
            ("code", "name", "startsOn", "endsOn"),
            "academic session",
        )
        return cleaned

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        upsert_academic_session(membership=ctx.membership, payload=stored)


class AcademicTermHandler(EntityHandler):
    entity_type = TERM_ENTITY
    roles = _ADMIN_ROLES

    def visible(self, membership, payload):
        return payload if membership.role in _READ_ROLES else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        external_id = text(p, "id", max_len=64)
        if external_id != ctx.entity_id:
            raise Rejected("id must match the academic-term entity id.")
        cleaned = {
            "id": external_id,
            "sessionId": text(p, "sessionId", max_len=64),
            "code": text(p, "code", max_len=40),
            "name": text(p, "name", max_len=80),
            "sequence": _positive_int(p, "sequence", maximum=20),
            "startsOn": text(p, "startsOn", max_len=10),
            "endsOn": text(p, "endsOn", max_len=10),
            "status": choice(p.get("status"), _SESSION_STATUSES, "status"),
        }
        _freeze_closed(
            ctx.existing,
            cleaned,
            ("sessionId", "code", "name", "sequence", "startsOn", "endsOn"),
            "academic term",
        )
        return cleaned

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        upsert_academic_term(membership=ctx.membership, payload=stored)


class AcademicClassHandler(EntityHandler):
    entity_type = CLASS_ENTITY
    roles = _ADMIN_ROLES

    def visible(self, membership, payload):
        return payload if membership.role in _READ_ROLES else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        external_id = text(p, "id", max_len=64)
        if external_id != ctx.entity_id:
            raise Rejected("id must match the academic-class entity id.")
        next_class_id = _optional_text(p, "nextClassId", max_len=64)
        if next_class_id == external_id:
            raise Rejected("A class cannot progress to itself. Use Repeat instead.")
        return {
            "id": external_id,
            "code": text(p, "code", max_len=60),
            "name": text(p, "name", max_len=120),
            "section": text(p, "section", max_len=80),
            "levelOrder": _positive_int(p, "levelOrder", maximum=1000),
            "stream": _optional_text(p, "stream", max_len=60),
            "nextClassId": next_class_id,
            "isTerminal": boolean(p, "isTerminal"),
            "isActive": boolean(p, "isActive"),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        upsert_academic_class(membership=ctx.membership, payload=stored)


class ProgressionBatchHandler(EntityHandler):
    entity_type = PROGRESSION_BATCH_ENTITY
    roles = _ADMIN_ROLES

    def visible(self, membership, payload):
        return payload if membership.role in _READ_ROLES else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        # Native SchoolOS creates UUID-form identifiers. Keeping this bounded also
        # keeps derived StudentLifecycleEvent and SyncRecord ids below 128 chars.
        external_id = text(p, "id", max_len=36)
        if external_id != ctx.entity_id:
            raise Rejected("id must match the progression-batch entity id.")
        status = choice(p.get("status"), _BATCH_STATUSES, "status")
        existing = ctx.existing
        if existing is not None:
            if existing.get("status") in {"applied", "cancelled"}:
                if status != existing.get("status"):
                    raise Rejected("Applied or cancelled progression batches are final.")
            for key in ("fromSessionId", "toSessionId", "sourceClassId"):
                if existing.get(key) != p.get(key):
                    raise Rejected(
                        "The source session, destination session and source class are immutable for a batch."
                    )

        raw_decisions = p.get("decisions")
        if not isinstance(raw_decisions, list):
            raise Rejected("decisions must be a list.")
        if len(raw_decisions) > 500:
            raise Rejected("A progression batch may contain at most 500 students.")
        decisions = []
        seen = set()
        for raw in raw_decisions:
            if not isinstance(raw, dict):
                raise Rejected("Each progression decision must be an object.")
            student_id = text(raw, "studentId", max_len=80)
            if student_id in seen:
                raise Rejected("Each student may appear only once in a progression batch.")
            seen.add(student_id)
            outcome = choice(raw.get("outcome"), _OUTCOMES, "outcome")
            decisions.append(
                {
                    "studentId": student_id,
                    "outcome": outcome,
                    "targetClassId": _optional_text(raw, "targetClassId", max_len=64),
                    "recordsPackReady": boolean(raw, "recordsPackReady"),
                    "note": _optional_text(raw, "note", max_len=1000),
                }
            )

        approved_by = _optional_text(p, "approvedBy", max_len=200)
        if status == ProgressionBatchStatus.APPLIED:
            if not approved_by:
                raise Rejected(
                    "Academic approver is required before applying a progression batch."
                )
            if not decisions:
                raise Rejected("A progression batch cannot be applied without decisions.")
            if any(item["outcome"] == ProgressionOutcome.HOLD for item in decisions):
                raise Rejected(
                    "Resolve every Hold decision before applying the progression batch."
                )

        return {
            "id": external_id,
            "fromSessionId": text(p, "fromSessionId", max_len=64),
            "toSessionId": text(p, "toSessionId", max_len=64),
            "sourceClassId": text(p, "sourceClassId", max_len=64),
            "status": status,
            "approvedBy": approved_by,
            "note": _optional_text(p, "note", max_len=2000),
            "decisions": decisions,
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        batch = upsert_progression_batch(membership=ctx.membership, payload=stored)
        canonical = serialize_progression_batch(batch)
        SyncRecord.objects.filter(
            school=ctx.membership.school,
            entity_type=self.entity_type,
            entity_id=ctx.entity_id,
        ).update(payload=canonical)


HANDLERS = [
    AcademicSessionHandler(),
    AcademicTermHandler(),
    AcademicClassHandler(),
    ProgressionBatchHandler(),
]
