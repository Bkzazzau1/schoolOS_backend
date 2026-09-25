"""A Driver's reported transport incident, and Transport Control's review of it."""

from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, text
from apps.sync.registry import EntityHandler, MutationContext

from . import constants as c
from . import daily_run
from . import shared


def _requires_escalation(category: str, severity: str) -> bool:
    return severity in ("critical", "high") or category == "accident"


class IncidentHandler(EntityHandler):
    """One row per reported incident (entity id: membershipId:incident:serviceDate:epoch).

    Only a Driver reports one, for today, on their real current route; what makes it urgent
    is the server's own rule (critical or high severity, or any accident), never a flag the
    device sends. A student named in it must really be on that route's manifest. Once
    reported, Transport Control (Proprietor/Administrator) moves it through acknowledged,
    under review and resolved - and nothing else about it can change; a resolved incident
    stays resolved.
    """

    entity_type = c.INCIDENT
    roles = frozenset({"driver"}) | c.MANAGERS

    def authorize(self, ctx: MutationContext) -> None:
        role = ctx.membership.role
        if ctx.operation == "delete":
            raise Rejected("A transport incident is never deleted, only resolved.")
        if ctx.operation == "create" and role != "driver":
            raise Rejected("Only the Driver who observed the incident reports it.")
        if ctx.operation == "update" and role not in c.MANAGERS:
            raise Rejected("Only Transport Control can change a reported incident.")
        super().authorize(ctx)

    def visible(self, membership, payload):
        if membership.role in c.READERS:
            return payload
        if membership.role == "driver" and payload.get("membershipId") == str(membership.id):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if ctx.existing is not None:
            return self._review(ctx, ctx.existing)
        return self._report(ctx)

    def _report(self, ctx: MutationContext) -> dict[str, Any]:
        p, school = ctx.payload, ctx.membership.school
        member_id = str(ctx.membership.id)
        parts = daily_run.require_own_entity(ctx, member_id, id_parts=4)
        if parts[1] != "incident" or not parts[3].isdigit():
            raise Rejected("This record does not belong to the active Driver assignment.")
        service_date = parts[2]
        daily_run.require_today(service_date)
        if text(p, "id", max_len=160) != ctx.entity_id:
            raise Rejected("id must match the record.")
        if text(p, "membershipId", max_len=64) != member_id:
            raise Rejected("membershipId must match the active Driver.")

        route_id = daily_run.require_driver_route(ctx)
        if text(p, "routeId", max_len=64) != route_id:
            raise Rejected("routeId must match the Driver's real current assignment.")
        route = shared.record(school, c.ROUTE, route_id) or {}

        category = choice(p.get("category"), c.INCIDENT_CATEGORIES, "category")
        severity = choice(p.get("severity"), c.INCIDENT_SEVERITIES, "severity")
        phase = choice(p.get("phase"), c.INCIDENT_PHASES, "phase")
        description = text(p, "description", max_len=2000)
        if len(description) < 10:
            raise Rejected("Add a short factual description of at least 10 characters.")

        student_id = text(p, "studentId", max_len=64, required=False)
        student_name = ""
        if student_id:
            rider = _rider_on_route(school, route_id, student_id)
            if rider is None:
                raise Rejected("The selected student is not on this Driver's assigned route manifest.")
            student_name = rider.get("studentName", "")

        return {
            "id": ctx.entity_id,
            "membershipId": member_id,
            "routeId": route_id,
            "vehicle": route.get("vehicle", ""),
            "serviceDate": service_date,
            "category": category,
            "severity": severity,
            "phase": phase,
            "description": description,
            "reportedAt": text(p, "reportedAt", max_len=40),
            "receivedAt": ctx.now,
            "locationNote": text(p, "locationNote", max_len=300, required=False),
            "studentId": student_id,
            "studentName": student_name,
            "status": "submitted",
            "requiresImmediateEscalation": _requires_escalation(category, severity),
            "serverReference": ctx.entity_id,
            "reportedByMembershipId": member_id,
            "requiresTransportReview": True,
            "updatedAt": ctx.now,
        }

    def _review(self, ctx: MutationContext, old: dict[str, Any]) -> dict[str, Any]:
        p = ctx.payload
        if old.get("status") == "resolved":
            raise Rejected("This incident is already resolved.")
        status = choice(p.get("status"), c.INCIDENT_REVIEW_STATUSES, "status")
        note = text(p, "managementNote", max_len=500, required=False)
        if status == "resolved" and not note:
            raise Rejected("Add a resolution note before closing this incident.")
        return {
            **old,
            "status": status,
            "managementNote": note or old.get("managementNote", ""),
            "updatedAt": ctx.now,
            "updatedByMembershipId": str(ctx.membership.id),
            "requiresTransportReview": status != "resolved",
        }


def _rider_on_route(school, route_id: str, student_id: str) -> dict | None:
    for payload in shared.records(school, c.RIDER_ASSIGNMENT):
        if payload.get("active") and payload.get("routeId") == route_id and payload.get("studentId") == student_id:
            return payload
    return None


class CaseEventHandler(EntityHandler):
    """Append-only: every acknowledge / review / resolve / clear Transport Control records
    against an incident or a vehicle defect."""

    entity_type = c.CASE_EVENT
    roles = c.MANAGERS

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation != "create":
            raise Rejected("A case event is never changed once recorded.")
        super().authorize(ctx)

    def visible(self, membership, payload):
        return payload if membership.role in c.READERS else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        stored = {
            "id": ctx.entity_id,
            "caseId": text(p, "caseId", max_len=200),
            "caseType": choice(p.get("caseType"), ("incident", "vehicle_defect"), "caseType"),
            "routeId": text(p, "routeId", max_len=64, required=False),
            "vehicle": text(p, "vehicle", max_len=120, required=False),
            "eventType": text(p, "eventType", max_len=64),
            "status": text(p, "status", max_len=40, required=False),
            "at": ctx.now,
            "occurredAt": shared.occurred_at(p),
            "actorMembershipId": str(ctx.membership.id),
            "actorRole": ctx.membership.role,
        }
        stored.update(shared.extra_event_detail(p, exclude=stored.keys()))
        return stored
