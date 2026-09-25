"""Which Driver is on which route today, and the audit trail of who changed it."""

from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import boolean, text
from apps.sync.registry import EntityHandler, MutationContext

from . import constants as c
from . import shared


class DriverAssignmentHandler(EntityHandler):
    """One row per Driver membership (the entity id): the route they are on, if any.

    Only Proprietor or Administrator change it. A route holds one active driver at a
    time, and once today's manifest or vehicle check exists for a driver or a route,
    that driver's assignment is locked for the rest of the service day - the same rule
    the app enforces client-side, kept here so no device can bypass it.
    """

    entity_type = c.DRIVER_ASSIGNMENT
    roles = c.MANAGERS

    def visible(self, membership, payload):
        if membership.role in c.READERS:
            return payload
        if membership.role == "driver" and payload.get("membershipId") == str(membership.id):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p, old, school = ctx.payload, ctx.existing, ctx.membership.school
        if text(p, "membershipId", max_len=64) != ctx.entity_id:
            raise Rejected("membershipId must match the record.")
        active = boolean(p, "active")
        route_id = text(p, "routeId", max_len=64, required=False)
        driver_name = text(p, "driverDisplayName", max_len=160)
        staff_id = text(p, "staffId", max_len=64, required=False)
        has_route = active and bool(route_id)

        old_has_route = bool(old and old.get("active") and old.get("routeId"))
        old_route_id = (old or {}).get("routeId", "")
        changing = (old_route_id if old_has_route else "") != (route_id if has_route else "")

        if changing:
            if old_has_route and shared.driver_has_service_state_today(school, ctx.entity_id):
                raise Rejected(
                    "Today's transport manifest or vehicle check already exists for this Driver. "
                    "The assignment is locked for the service day."
                )
            if has_route:
                if not shared.route_exists(school, route_id):
                    raise Rejected("That transport route does not exist.")
                conflicts = shared.active_assignments_for_route(school, route_id, exclude_membership_id=ctx.entity_id)
                if conflicts:
                    raise Rejected("This route is already assigned to another Driver.")
                if shared.route_locked_today(school, route_id, except_membership_id=ctx.entity_id):
                    raise Rejected(
                        "This route already has transport preparation or activity today under another Driver."
                    )

        return {
            "membershipId": ctx.entity_id,
            "routeId": route_id if active else "",
            "driverDisplayName": driver_name,
            "staffId": staff_id,
            "active": active,
            "assignedAt": ctx.now,
            "assignedByMembershipId": str(ctx.membership.id),
        }


class AssignmentEventHandler(EntityHandler):
    """Append-only: every driver_route_assigned / driver_route_unassigned change."""

    entity_type = c.ASSIGNMENT_EVENT
    roles = c.MANAGERS

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation != "create":
            raise Rejected("An assignment event is never changed once recorded.")
        super().authorize(ctx)

    def visible(self, membership, payload):
        return payload if membership.role in c.READERS else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        event_type = text(p, "eventType", max_len=64)
        return {
            "id": ctx.entity_id,
            "eventType": event_type,
            "driverMembershipId": text(p, "driverMembershipId", max_len=64),
            "driverName": text(p, "driverName", max_len=160, required=False),
            "staffId": text(p, "staffId", max_len=64, required=False),
            "previousRouteId": text(p, "previousRouteId", max_len=64, required=False),
            "newRouteId": text(p, "newRouteId", max_len=64, required=False),
            "at": ctx.now,
            "actorMembershipId": str(ctx.membership.id),
            "actorRole": ctx.membership.role,
        }
