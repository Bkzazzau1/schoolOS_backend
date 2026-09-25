"""Which student rides which route and stop, and the audit trail of changes."""

from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import boolean, text
from apps.sync.registry import EntityHandler, MutationContext

from . import constants as c
from . import shared


class RiderAssignmentHandler(EntityHandler):
    """One row per student (the entity id): the route and stop they board at, if any.

    Only Proprietor or Administrator change it, and only onto a stop that is really
    active on that route's stop plan. Once today's service has started for a route,
    moving a rider off or onto it is locked for the rest of the day.
    """

    entity_type = c.RIDER_ASSIGNMENT
    roles = c.MANAGERS

    def visible(self, membership, payload):
        if membership.role in c.READERS:
            return payload
        if membership.role == "driver":
            own = shared.active_assignment_for_driver(membership.school, str(membership.id))
            if own and own.get("routeId") == payload.get("routeId") and payload.get("active"):
                return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p, old, school = ctx.payload, ctx.existing, ctx.membership.school
        if text(p, "studentId", max_len=64) != ctx.entity_id:
            raise Rejected("studentId must match the record.")
        student_name = text(p, "studentName", max_len=160)
        class_name = text(p, "className", max_len=80, required=False)
        active = boolean(p, "active")
        route_id = text(p, "routeId", max_len=64, required=False)
        stop_id = text(p, "stopId", max_len=96, required=False)
        has_route = active and bool(route_id) and bool(stop_id)

        old_route_id = (old or {}).get("routeId", "") if (old or {}).get("active") else ""
        new_route_id = route_id if has_route else ""

        if old_route_id and old_route_id != new_route_id:
            if shared.route_locked_today(school, old_route_id):
                raise Rejected(
                    "The student's current route is locked because today's transport "
                    "preparation or service has already started."
                )
        if new_route_id and new_route_id != old_route_id:
            if not shared.route_exists(school, new_route_id):
                raise Rejected("That transport route does not exist.")
            plan = shared.record(school, c.ROUTE_PLAN, new_route_id)
            if shared.active_stop(plan, stop_id) is None:
                raise Rejected("The selected stop is not active on this route.")
            if shared.route_locked_today(school, new_route_id):
                raise Rejected(
                    "The target route is locked because today's transport preparation "
                    "or service has already started."
                )

        return {
            "studentId": ctx.entity_id,
            "studentName": student_name,
            "className": class_name,
            "routeId": route_id if has_route else "",
            "stopId": stop_id if has_route else "",
            "active": active,
            "assignedAt": ctx.now,
            "assignedByMembershipId": str(ctx.membership.id),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        touched = {stored.get("routeId")}
        if ctx.existing:
            touched.add(ctx.existing.get("routeId"))
        for route_id in touched:
            if route_id:
                _sync_rider_count(ctx.membership.school, route_id, ctx.membership)


def _sync_rider_count(school, route_id: str, actor) -> None:
    count = sum(
        1
        for payload in shared.records(school, c.RIDER_ASSIGNMENT)
        if payload.get("active") and payload.get("routeId") == route_id
    )
    route = shared.record(school, c.ROUTE, route_id)
    if route is None or route.get("riders") == count:
        return
    shared.replace_payload(school, c.ROUTE, route_id, {**route, "riders": count}, actor)


class RiderAssignmentEventHandler(EntityHandler):
    """Append-only: every student_transport_assigned / _reassigned / _unassigned change."""

    entity_type = c.RIDER_ASSIGNMENT_EVENT
    roles = c.MANAGERS

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation != "create":
            raise Rejected("A rider assignment event is never changed once recorded.")
        super().authorize(ctx)

    def visible(self, membership, payload):
        return payload if membership.role in c.READERS else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        return {
            "id": ctx.entity_id,
            "studentId": text(p, "studentId", max_len=64),
            "eventType": text(p, "eventType", max_len=64),
            "previousRouteId": text(p, "previousRouteId", max_len=64, required=False),
            "previousStopId": text(p, "previousStopId", max_len=96, required=False),
            "newRouteId": text(p, "newRouteId", max_len=64, required=False),
            "newStopId": text(p, "newStopId", max_len=96, required=False),
            "at": ctx.now,
            "actorMembershipId": str(ctx.membership.id),
            "actorRole": ctx.membership.role,
        }
