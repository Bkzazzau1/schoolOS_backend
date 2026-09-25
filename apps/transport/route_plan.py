"""A route's own stop plan: which stops exist, their order and their pickup/drop times."""

import re
from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import boolean, integer, text
from apps.sync.registry import EntityHandler, MutationContext

from . import constants as c
from . import shared

_TIME = re.compile(c.TIME_PATTERN)


def _clean_stop(raw: Any, *, index: int) -> dict:
    if not isinstance(raw, dict):
        raise Rejected(f"Stop {index + 1} must be an object.")
    stop_id = text(raw, "id", max_len=128)
    name = text(raw, "name", max_len=120)
    morning = text(raw, "morningTime", max_len=16)
    afternoon = text(raw, "afternoonTime", max_len=16)
    if not _TIME.match(morning) or not _TIME.match(afternoon):
        raise Rejected(f"{name}: morning and afternoon times must be HH:mm.")
    return {
        "id": stop_id,
        "sequence": integer(raw, "sequence", minimum=1, maximum=200),
        "name": name,
        "morningTime": morning,
        "afternoonTime": afternoon,
        "active": bool(raw.get("active", True)),
    }


class RoutePlanHandler(EntityHandler):
    """One row per route (the entity id): its stops, in order.

    Only Proprietor or Administrator change it, and only while the route has no
    service activity today. A stop that still has a rider assigned to it cannot be
    deactivated - reassign or remove those riders first.
    """

    entity_type = c.ROUTE_PLAN
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
        p, old, school = ctx.payload, ctx.existing, ctx.membership.school
        route_id = text(p, "routeId", max_len=64)
        if route_id != ctx.entity_id:
            raise Rejected("routeId must match the record.")
        if not shared.route_exists(school, route_id):
            raise Rejected("That transport route does not exist.")

        raw_stops = p.get("stops")
        if not isinstance(raw_stops, list) or len(raw_stops) > 100:
            raise Rejected("stops must be a list of at most 100 stops.")
        stops = [_clean_stop(item, index=i) for i, item in enumerate(raw_stops)]

        active = [s for s in stops if s["active"]]
        seen_names = set()
        for stop in active:
            key = stop["name"].strip().lower()
            if key in seen_names:
                raise Rejected(f'This route already contains a stop named "{stop["name"]}".')
            seen_names.add(key)

        if shared.route_locked_today(school, route_id):
            raise Rejected(
                "This route is locked because today's transport preparation or service has already started."
            )

        old_active_ids = {s["id"] for s in (old or {}).get("stops", []) if s.get("active", True)}
        new_active_ids = {s["id"] for s in active}
        for deactivated_id in old_active_ids - new_active_ids:
            if _stop_has_riders(school, route_id, deactivated_id):
                raise Rejected(
                    "Students are still assigned to a stop being removed. Reassign or remove those riders first."
                )

        return {
            "routeId": route_id,
            "stops": stops,
            "updatedAt": ctx.now,
            "updatedByMembershipId": str(ctx.membership.id),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        count = len([s for s in stored.get("stops", []) if s.get("active")])
        route = shared.record(ctx.membership.school, c.ROUTE, stored["routeId"])
        if route is not None and route.get("stops") != count:
            shared.replace_payload(
                ctx.membership.school, c.ROUTE, stored["routeId"], {**route, "stops": count}, ctx.membership
            )


def _stop_has_riders(school, route_id: str, stop_id: str) -> bool:
    return any(
        payload.get("active") and payload.get("routeId") == route_id and payload.get("stopId") == stop_id
        for payload in shared.records(school, c.RIDER_ASSIGNMENT)
    )


class RouteEventHandler(EntityHandler):
    """Append-only: every route_created / route_details_updated / route_stop_* change."""

    entity_type = c.ROUTE_EVENT
    roles = c.MANAGERS

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation != "create":
            raise Rejected("A route event is never changed once recorded.")
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
