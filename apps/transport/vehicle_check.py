"""A Driver's own pre-trip vehicle check, and the safety defects a failed item reports."""

from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, text
from apps.sync.registry import EntityHandler, MutationContext

from . import constants as c
from . import daily_run
from . import shared

_CHECK_STATUSES = {"notStarted", "inProgress", "ready", "blocked"}
_ITEM_STATUSES = {"unchecked", "passed", "failed"}
_MANAGERS = c.MANAGERS


class VehicleCheckHandler(EntityHandler):
    """One row per Driver per service day per period
    (entity id: membershipId:vehicle-check:serviceDate:period).

    Only the Driver it belongs to writes it, for today, on their real current route. Its
    item catalog and each item's severity are the server's own fixed list (see
    constants.VEHICLE_CHECK_ITEM_SEVERITY) - a device reports pass/fail/a note, never a
    severity, since severity decides whether a failure blocks the vehicle's release.
    """

    entity_type = c.VEHICLE_CHECK
    roles = frozenset({"driver"})

    def visible(self, membership, payload):
        if membership.role in c.READERS:
            return payload
        if membership.role == "driver" and payload.get("membershipId") == str(membership.id):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p, school = ctx.payload, ctx.membership.school
        member_id = str(ctx.membership.id)
        parts = daily_run.require_own_entity(ctx, member_id, id_parts=4)
        if parts[1] != "vehicle-check":
            raise Rejected("This record does not belong to the active Driver assignment.")
        service_date, period = parts[2], parts[3]
        if period not in ("morning", "afternoon"):
            raise Rejected("period must be morning or afternoon.")
        daily_run.require_today(service_date)

        route_id = daily_run.require_driver_route(ctx)
        route = shared.record(school, c.ROUTE, route_id) or {}

        if text(p, "membershipId", max_len=64) != member_id:
            raise Rejected("membershipId must match the active Driver.")
        if text(p, "routeId", max_len=64) != route_id:
            raise Rejected("routeId must match the Driver's real current assignment.")
        if choice(p.get("period"), ("morning", "afternoon"), "period") != period:
            raise Rejected("period must match the record.")
        if text(p, "serviceDate", max_len=10) != service_date:
            raise Rejected("serviceDate must match the record.")

        items = _clean_items(p.get("items"))
        status = choice(p.get("status", "notStarted"), _CHECK_STATUSES, "status")

        return {
            "id": ctx.entity_id,
            "membershipId": member_id,
            "routeId": route_id,
            "vehicle": route.get("vehicle", ""),
            "serviceDate": service_date,
            "period": period,
            "items": items,
            "status": status,
            "submittedAt": text(p, "submittedAt", max_len=40, required=False),
        }

def _clean_items(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        raise Rejected("items must be a list.")
    by_id = {}
    for item in raw:
        if not isinstance(item, dict):
            raise Rejected("Each check item must be an object.")
        item_id = item.get("id")
        if item_id not in c.VEHICLE_CHECK_ITEM_IDS or item_id in by_id:
            raise Rejected(f"{item_id!r} is not a real vehicle check item.")
        status = choice(item.get("status", "unchecked"), _ITEM_STATUSES, "status")
        note = text(item, "note", max_len=500, required=False)
        if status == "failed" and not note:
            raise Rejected(f"{item_id}: add a short note describing the observed defect.")
        by_id[item_id] = {
            "id": item_id,
            "label": str(item.get("label", ""))[:160],
            "description": str(item.get("description", ""))[:500],
            "severity": c.VEHICLE_CHECK_ITEM_SEVERITY[item_id],
            "status": status,
            "note": note if status == "failed" else "",
            "updatedAt": str(item.get("updatedAt", "")),
        }
    if set(by_id) != c.VEHICLE_CHECK_ITEM_IDS:
        raise Rejected("The vehicle check must include every real check item, once each.")
    return list(by_id.values())


class VehicleDefectHandler(EntityHandler):
    """One row per (check, failed item): reported by the Driver who observed it, and
    only Transport Control (Proprietor/Administrator) can move it toward resolved."""

    entity_type = c.VEHICLE_DEFECT
    roles = frozenset({"driver"}) | _MANAGERS

    def authorize(self, ctx: MutationContext) -> None:
        role = ctx.membership.role
        if ctx.operation == "delete":
            raise Rejected("A vehicle defect is never deleted, only resolved.")
        if ctx.operation == "create" and role != "driver":
            raise Rejected("Only the Driver who observed the defect reports it.")
        super().authorize(ctx)

    def visible(self, membership, payload):
        if membership.role in c.READERS:
            return payload
        if membership.role == "driver" and payload.get("reportedByMembershipId") == str(membership.id):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p, old = ctx.payload, ctx.existing
        if old is not None:
            return self._update(ctx, old)

        member_id = str(ctx.membership.id)
        check_id = text(p, "checkId", max_len=160)
        check_parts = check_id.split(":")
        if len(check_parts) != 4 or check_parts[0] != member_id or check_parts[1] != "vehicle-check":
            raise Rejected("checkId must be this Driver's own vehicle check.")
        item_id = text(p, "itemId", max_len=64)
        if item_id not in c.VEHICLE_CHECK_ITEM_IDS:
            raise Rejected("itemId is not a real vehicle check item.")
        if ctx.entity_id != f"{check_id}:{item_id}":
            raise Rejected("id must be checkId:itemId.")
        daily_run.require_today(check_parts[2])

        route_id = daily_run.require_driver_route(ctx)
        route = shared.record(ctx.membership.school, c.ROUTE, route_id) or {}
        severity = c.VEHICLE_CHECK_ITEM_SEVERITY[item_id]

        return {
            "id": ctx.entity_id,
            "checkId": check_id,
            "routeId": route_id,
            "vehicle": route.get("vehicle", ""),
            "serviceDate": check_parts[2],
            "period": check_parts[3],
            "itemId": item_id,
            "itemLabel": text(p, "itemLabel", max_len=160, required=False),
            "severity": severity,
            "blocksTrip": severity == "critical",
            "note": text(p, "note", max_len=500),
            "status": "reported",
            "reportedAt": ctx.now,
            "reportedByMembershipId": member_id,
        }

    def _update(self, ctx: MutationContext, old: dict[str, Any]) -> dict[str, Any]:
        p, role, member_id = ctx.payload, ctx.membership.role, str(ctx.membership.id)
        if role == "driver":
            # The app re-sends a defect when the same failed item is submitted again. That must
            # never undo Transport Control's progress on it, so once anyone has acted on it the
            # driver's re-send changes nothing; while it is still just reported, the note may be
            # refreshed. Only the Driver who reported it may do even that.
            if old.get("reportedByMembershipId") != member_id:
                raise Rejected("Only the Driver who reported this defect may re-send it.")
            if old.get("status") != "reported":
                return old
            return {**old, "note": text(p, "note", max_len=500)}
        if role not in _MANAGERS:
            raise Rejected("Only Transport Control can change a reported defect.")

        # Transport Control moves it forward; everything about what was observed stays fixed.
        if str(old.get("status", "")).lower() in c.DEFECT_CLOSED:
            raise Rejected("This vehicle defect is already closed.")
        status = choice(p.get("status"), c.DEFECT_STATUSES, "status")
        if status == "reported":
            raise Rejected("A defect that has been acted on cannot go back to reported.")
        note = text(p, "managementNote", max_len=500, required=False)
        closing = status in c.DEFECT_CLOSED
        if closing and not note:
            raise Rejected("Add a clearance note describing the completed safety action.")
        stored = {
            **old,
            "status": status,
            "managementNote": note or old.get("managementNote", ""),
            "updatedAt": ctx.now,
            "updatedByMembershipId": member_id,
            "requiresTransportReview": not closing,
        }
        if closing:
            stored["clearedAt"] = ctx.now
            stored["clearedByMembershipId"] = member_id
        return stored
