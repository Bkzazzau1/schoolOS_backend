"""Cross-record checks every transport handler needs: does this route exist, is a driver already
on it, has today's service already started for a driver or route. Queried straight from SyncRecord
so a check works even for an entity type (a run, a vehicle check) that has no handler of its own yet.
"""

from django.utils import timezone

from apps.core.errors import Rejected
from apps.sync.models import SyncRecord

from . import constants as c


#: An audit event carries a few extra fields depending on what happened (a route's new
#: name, a stop's sequence, a status). Kept flat, the same shape the app sends, but only
#: simple scalar values up to a modest size - never a nested structure a device could use
#: to smuggle an oversized or malformed record into an append-only log.
def extra_event_detail(payload: dict, *, exclude) -> dict:
    extra = {}
    for key, value in payload.items():
        if key in exclude:
            continue
        if isinstance(value, str):
            if len(value) > 500:
                raise Rejected(f"{key} is too long.")
            extra[key] = value
        elif isinstance(value, (int, float, bool)) or value is None:
            extra[key] = value
    return extra


def occurred_at(payload: dict, key: str = "at") -> str:
    """When the app says something happened. An offline device syncs later, so the server's own
    `at` is only when it *arrived*; this keeps the moment the driver or manager really acted,
    as a claim, next to it - never used to decide anything."""
    value = payload.get(key)
    return value.strip()[:40] if isinstance(value, str) else ""


def replace_payload(school, entity_type: str, entity_id: str, payload: dict, actor=None) -> None:
    """Server-derived write to a record another handler owns (the route's own rider/stop
    counts). Never used for a device's own change - only for keeping a derived field
    consistent right after a related record is written."""
    SyncRecord.objects.filter(school=school, entity_type=entity_type, entity_id=entity_id).update(
        payload=payload, deleted=False, updated_by=actor
    )


def today_key() -> str:
    """The school day, in the school's own timezone, the same way the app keys a run or check."""
    return timezone.localdate().isoformat()


def record(school, entity_type: str, entity_id: str) -> dict | None:
    row = SyncRecord.objects.filter(
        school=school, entity_type=entity_type, entity_id=entity_id, deleted=False
    ).first()
    return row.payload if row else None


def records(school, entity_type: str) -> list[dict]:
    return [
        row.payload
        for row in SyncRecord.objects.filter(school=school, entity_type=entity_type, deleted=False)
    ]


def route_exists(school, route_id: str) -> bool:
    return record(school, c.ROUTE, route_id) is not None


def active_assignment_for_driver(school, membership_id: str) -> dict | None:
    payload = record(school, c.DRIVER_ASSIGNMENT, membership_id)
    if payload and payload.get("active") and payload.get("routeId"):
        return payload
    return None


def active_assignments_for_route(school, route_id: str, *, exclude_membership_id: str = "") -> list[dict]:
    result = []
    for payload in records(school, c.DRIVER_ASSIGNMENT):
        if not payload.get("active") or payload.get("routeId") != route_id:
            continue
        if payload.get("membershipId") == exclude_membership_id:
            continue
        result.append(payload)
    return result


def driver_has_service_state_today(school, membership_id: str) -> bool:
    today = today_key()
    if record(school, c.MORNING_RUN, f"{membership_id}:morning:{today}"):
        return True
    if record(school, c.AFTERNOON_RUN, f"{membership_id}:afternoon:{today}"):
        return True
    for period in ("morning", "afternoon"):
        if record(school, c.VEHICLE_CHECK, f"{membership_id}:vehicle-check:{today}:{period}"):
            return True
    return False


def route_locked_today(school, route_id: str, *, except_membership_id: str = "") -> bool:
    """A route is locked once today's manifest or vehicle check exists for it under any driver
    other than the one being excluded (the driver whose own assignment is being changed)."""
    today = today_key()
    for payload in records(school, c.MORNING_RUN):
        if payload.get("routeId") == route_id and payload.get("serviceDate") == today:
            if payload.get("membershipId") != except_membership_id:
                return True
    for payload in records(school, c.AFTERNOON_RUN):
        if payload.get("routeId") == route_id and payload.get("serviceDate") == today:
            if payload.get("membershipId") != except_membership_id:
                return True
    for payload in records(school, c.VEHICLE_CHECK):
        if payload.get("routeId") == route_id and payload.get("serviceDate") == today:
            if payload.get("membershipId") != except_membership_id:
                return True
    return False


def active_stop(plan_payload: dict | None, stop_id: str) -> dict | None:
    if not plan_payload:
        return None
    for stop in plan_payload.get("stops", []):
        if stop.get("id") == stop_id and stop.get("active", True):
            return stop
    return None


def open_blocking_defects(school, route_id: str, vehicle: str) -> int:
    closed = {"cleared", "resolved", "closed"}
    count = 0
    for payload in records(school, c.VEHICLE_DEFECT):
        if payload.get("routeId") != route_id or payload.get("vehicle") != vehicle:
            continue
        if str(payload.get("status", "")).lower() in closed:
            continue
        if payload.get("blocksTrip"):
            count += 1
    return count
