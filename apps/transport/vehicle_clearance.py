"""Whether a route's vehicle is released for service, held, or in maintenance."""

from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, text
from apps.sync.registry import EntityHandler, MutationContext

from . import constants as c
from . import shared


class VehicleClearanceHandler(EntityHandler):
    """One row per route (the entity id): Transport Control's release decision for its vehicle.

    Only Proprietor or Administrator change it. Releasing a vehicle is refused while
    it still has an open, trip-blocking safety defect; holding or maintaining it always
    needs a short operational reason.
    """

    entity_type = c.VEHICLE_CLEARANCE
    roles = c.MANAGERS

    def visible(self, membership, payload):
        if membership.role in c.READERS:
            return payload
        if membership.role == "driver":
            own = shared.active_assignment_for_driver(membership.school, str(membership.id))
            if own and own.get("routeId") == payload.get("routeId"):
                return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p, school = ctx.payload, ctx.membership.school
        route_id = text(p, "routeId", max_len=64)
        if route_id != ctx.entity_id:
            raise Rejected("routeId must match the record.")
        route = shared.record(school, c.ROUTE, route_id)
        if route is None:
            raise Rejected("That transport route does not exist.")

        status = choice(p.get("status"), c.CLEARANCE_STATUSES, "status")
        note = text(p, "note", max_len=500, required=False)
        if status != "released" and not note:
            raise Rejected("Add a short operational reason before holding this vehicle.")

        vehicle = route.get("vehicle", "")
        if status == "released":
            blocking = shared.open_blocking_defects(school, route_id, vehicle)
            if blocking:
                raise Rejected(
                    f"This vehicle still has {blocking} open trip-blocking safety "
                    "defect(s). Clear the defect before releasing it."
                )

        return {
            "routeId": route_id,
            "vehicle": vehicle,
            "status": status,
            "note": note,
            "updatedAt": ctx.now,
            "updatedByMembershipId": str(ctx.membership.id),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        # The route's own status only distinguishes available from maintenance, so a held
        # or maintained vehicle must also flip it - otherwise a vehicle Transport Control
        # just held still looks assignable to a new driver or rider.
        route = shared.record(ctx.membership.school, c.ROUTE, stored["routeId"])
        if route is None:
            return
        current = route.get("status")
        if stored["status"] in ("held", "maintenance"):
            next_status = "maintenance"
        elif stored["status"] == "released" and current == "maintenance":
            next_status = "preparing"
        else:
            next_status = current
        if next_status != current:
            shared.replace_payload(
                ctx.membership.school, c.ROUTE, stored["routeId"], {**route, "status": next_status}, ctx.membership
            )


class VehicleEventHandler(EntityHandler):
    """Append-only: every vehicle_clearance_changed and related vehicle event."""

    entity_type = c.VEHICLE_EVENT
    roles = c.MANAGERS

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation != "create":
            raise Rejected("A vehicle event is never changed once recorded.")
        super().authorize(ctx)

    def visible(self, membership, payload):
        return payload if membership.role in c.READERS else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        stored = {
            "id": ctx.entity_id,
            "routeId": text(p, "routeId", max_len=64),
            "vehicle": text(p, "vehicle", max_len=120, required=False),
            "eventType": text(p, "eventType", max_len=64),
            "at": ctx.now,
            "occurredAt": shared.occurred_at(p),
            "actorMembershipId": str(ctx.membership.id),
            "actorRole": ctx.membership.role,
        }
        stored.update(shared.extra_event_detail(p, exclude=stored.keys()))
        return stored
