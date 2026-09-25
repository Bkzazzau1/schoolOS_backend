"""What a Driver's own morning/afternoon run and vehicle check share: they belong to
exactly one driver, on exactly today's real assigned route, and their manifest (which
stops, which riders) is fixed to what Transport Control's own records say it should be -
a device can move a rider or stop through its states, but never invent one that was
never really there.
"""

from typing import Any

from apps.core.errors import Rejected

from . import constants as c
from . import shared


def require_driver_route(ctx) -> str:
    """The route this Driver is really, currently assigned to - never taken from the
    payload, since a stale or edited device could claim any route otherwise."""
    assignment = shared.active_assignment_for_driver(ctx.membership.school, str(ctx.membership.id))
    if assignment is None:
        raise Rejected("No active transport route is assigned to this Driver.")
    return assignment["routeId"]


def require_own_entity(ctx, expected_membership_id: str, id_parts: int) -> list[str]:
    parts = ctx.entity_id.split(":")
    if len(parts) != id_parts or parts[0] != expected_membership_id:
        raise Rejected("This record does not belong to the active Driver assignment.")
    return parts


def require_today(service_date: str) -> None:
    if service_date != shared.today_key():
        raise Rejected("serviceDate must be today.")


def clean_stops_shape(raw: Any) -> list[dict]:
    """Structural cleaning only - what each stop and rider must look like. Whether the
    stops and riders are the *real* ones is checked separately (see below), since that
    check differs between a fresh run (built from today's real manifest) and an update
    to one that already exists (must not have gained or lost a stop or rider)."""
    if not isinstance(raw, list) or not raw:
        raise Rejected("stops must be a non-empty list.")
    result, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise Rejected("Each stop must be an object.")
        stop_id = str(item.get("id", "")).strip()
        if not stop_id or stop_id in seen:
            raise Rejected("Each stop needs a real, unique id.")
        seen.add(stop_id)
        result.append({
            "id": stop_id,
            "sequence": item.get("sequence") if isinstance(item.get("sequence"), int) else 0,
            "name": str(item.get("name", ""))[:120],
            "scheduledTime": str(item.get("scheduledTime", ""))[:16],
            "riders": _clean_riders_shape(item.get("riders"), stop_id=stop_id),
            "status": str(item.get("status", "pending"))[:40],
            "arrivedAt": str(item.get("arrivedAt", "")),
            "departedAt": str(item.get("departedAt", "")),
        })
    return result


def _clean_riders_shape(raw: Any, *, stop_id: str) -> list[dict]:
    if not isinstance(raw, list):
        raise Rejected("riders must be a list.")
    result, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise Rejected("Each rider must be an object.")
        student_id = str(item.get("studentId", "")).strip()
        if not student_id or student_id in seen:
            raise Rejected("Each rider needs a real, unique studentId.")
        seen.add(student_id)
        result.append({
            "studentId": student_id,
            "name": str(item.get("name", ""))[:160],
            "className": str(item.get("className", ""))[:80],
            "stopId": stop_id,
            "status": str(item.get("status", "pending"))[:40],
            "note": str(item.get("note", ""))[:500],
            "updatedAt": str(item.get("updatedAt", "")),
        })
    return result


def require_real_manifest(school, route_id: str, stops: list[dict]) -> None:
    """A freshly created run: every stop must be a real active stop on the route's own
    plan, and its riders must be exactly the students really, actively assigned there -
    a device cannot start a run for a manifest Transport Control never configured."""
    plan = shared.record(school, c.ROUTE_PLAN, route_id) or {}
    valid_stop_ids = {s["id"] for s in plan.get("stops", []) if s.get("active", True)}
    riders_by_student = {
        p["studentId"]: p["stopId"]
        for p in shared.records(school, c.RIDER_ASSIGNMENT)
        if p.get("active") and p.get("routeId") == route_id
    }
    expected_by_stop: dict[str, set] = {}
    for student_id, stop_id in riders_by_student.items():
        expected_by_stop.setdefault(stop_id, set()).add(student_id)

    for stop in stops:
        if stop["id"] not in valid_stop_ids:
            raise Rejected(f'{stop["id"]!r} is not a real active stop on this route.')
        actual = {r["studentId"] for r in stop["riders"]}
        expected = expected_by_stop.get(stop["id"], set())
        if actual != expected:
            raise Rejected(f'The rider list for stop {stop["id"]!r} does not match the real assignments.')


def require_stable_manifest(old_stops: list[dict], new_stops: list[dict]) -> None:
    """An update to a run that already exists: the set of stops and the set of riders at
    each stop must stay exactly what it already was - only status/note/timestamp fields
    may change. Nothing here can add or remove a stop or a rider mid-run."""
    old_by_id = {s["id"]: s for s in old_stops}
    new_by_id = {s["id"]: s for s in new_stops}
    if set(old_by_id) != set(new_by_id):
        raise Rejected("This run's stops cannot change once it has started.")
    for stop_id, old_stop in old_by_id.items():
        old_riders = {r["studentId"] for r in old_stop.get("riders", [])}
        new_riders = {r["studentId"] for r in new_by_id[stop_id].get("riders", [])}
        if old_riders != new_riders:
            raise Rejected("This run's riders cannot change once it has started.")
