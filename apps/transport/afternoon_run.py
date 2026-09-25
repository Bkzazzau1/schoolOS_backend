"""A Driver's own afternoon run: boarding at school, stop by stop back home."""

from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import text
from apps.sync.registry import EntityHandler, MutationContext

from . import constants as c
from . import daily_run
from . import shared

_RUN_STATUSES = {"notStarted", "boarding", "inProgress", "returnedSchool", "completed"}


class AfternoonRunHandler(EntityHandler):
    """One row per Driver per service day (entity id: membershipId:afternoon:serviceDate).

    Same rules as the morning run (see daily_run.py) - only the owning Driver writes it,
    only for today's real assignment, and its stops/riders are fixed to the route's real
    manifest once created.
    """

    entity_type = c.AFTERNOON_RUN
    roles = frozenset({"driver"})

    def visible(self, membership, payload):
        if membership.role in c.READERS:
            return payload
        if membership.role == "driver" and payload.get("membershipId") == str(membership.id):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p, old, school = ctx.payload, ctx.existing, ctx.membership.school
        member_id = str(ctx.membership.id)
        daily_run.require_own_entity(ctx, member_id, id_parts=3)
        if text(p, "membershipId", max_len=64) != member_id:
            raise Rejected("membershipId must match the active Driver.")

        route_id = daily_run.require_driver_route(ctx)
        if text(p, "routeId", max_len=64) != route_id:
            raise Rejected("routeId must match the Driver's real current assignment.")

        service_date = text(p, "serviceDate", max_len=10)
        daily_run.require_today(service_date)
        if ctx.entity_id != f"{member_id}:afternoon:{service_date}":
            raise Rejected("The record id must match membershipId:afternoon:serviceDate.")

        route = shared.record(school, c.ROUTE, route_id) or {}
        vehicle = text(p, "vehicle", max_len=120, required=False)
        if vehicle and vehicle != route.get("vehicle"):
            raise Rejected("vehicle no longer matches the assigned route. Reload today's run.")

        status = p.get("status", "notStarted")
        if status not in _RUN_STATUSES:
            raise Rejected("status is not a valid choice.")

        stops = daily_run.clean_stops_shape(p.get("stops"))
        if old is None:
            daily_run.require_real_manifest(school, route_id, stops)
        else:
            daily_run.require_stable_manifest(old.get("stops", []), stops)

        return {
            "id": ctx.entity_id,
            "membershipId": member_id,
            "routeId": route_id,
            "serviceDate": service_date,
            "vehicle": route.get("vehicle", vehicle),
            "driverName": text(p, "driverName", max_len=160, required=False),
            "assistantName": text(p, "assistantName", max_len=160, required=False),
            "stops": stops,
            "status": status,
            "startedAt": text(p, "startedAt", max_len=40, required=False),
            "departedSchoolAt": text(p, "departedSchoolAt", max_len=40, required=False),
            "returnedSchoolAt": text(p, "returnedSchoolAt", max_len=40, required=False),
            "completedAt": text(p, "completedAt", max_len=40, required=False),
        }
