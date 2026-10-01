"""What passes between a Driver and Transport Control: a real, two-way message on their one real
shared channel, a real "thread seen" receipt either side may record for themselves, a real
operational alert Transport Control broadcasts to every real Driver, and a Driver's own "alert
read" receipt for it.

The message channel is genuinely two-way: a Driver writes from their own Messages screen, and
Transport Control (apps/transport/constants.py MANAGERS - Proprietor or Administrator, the same
role set that already sets every other transport policy in this module) may write back into that
same Driver's thread, the same `roles = {"driver"} | c.MANAGERS` shape `incident.py` already uses
for a Driver-reported, management-reviewed record. Principal keeps its existing read-only
oversight (READERS). A thread-seen receipt follows the same shape: whichever real side actually
opened the thread - the Driver, or Transport Control - records their own receipt, which is what
lets a thread's own `unread` state be real instead of always false.
"""

from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import text
from apps.sync.registry import EntityHandler, MutationContext

from . import constants as c
from . import daily_run, shared

_MAX_BODY = 2000
_MAX_TITLE = 200
_MAX_SCOPE_LABEL = 120


class _DriverAppendOnly(EntityHandler):
    """Driver-written, create-only, readable by the Driver who wrote it and by Transport Control."""

    roles = frozenset({"driver"})
    author_field = "membershipId"

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation != "create":
            raise Rejected("This record is never changed once recorded.")
        super().authorize(ctx)

    def visible(self, membership, payload):
        if membership.role in c.READERS:
            return payload
        if membership.role == "driver" and payload.get(self.author_field) == str(membership.id):
            return payload
        return None


class DriverMessageHandler(EntityHandler):
    """A real message on the one real channel between a Driver and Transport Control - one thread
    per Driver (entity id: LOCAL-<sender membershipId>-epoch), not per route, since a Driver's
    real assigned route can change without starting a new conversation. Who the real Driver is
    when Transport Control writes is checked against a real, currently active driver_transport_assignment
    - never taken on trust from the app."""

    entity_type = c.DRIVER_MESSAGE
    roles = frozenset({"driver"}) | c.MANAGERS
    allow_delete = False

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.membership.role not in self.roles:
            raise Rejected("Your role may not send transport messages.")
        if ctx.operation != "create":
            raise Rejected("This record is never changed once recorded.")

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        member_id = str(ctx.membership.id)
        prefix = f"LOCAL-{member_id}-"
        if not ctx.entity_id.startswith(prefix) or not ctx.entity_id[len(prefix):].isdigit():
            raise Rejected("This message does not belong to the active sender.")
        if text(p, "messageId", max_len=160) != ctx.entity_id:
            raise Rejected("messageId must match the record.")

        if ctx.membership.role == "driver":
            driver_membership_id = member_id
            route_id = daily_run.require_driver_route(ctx)
        else:
            driver_membership_id = text(p, "driverMembershipId", max_len=64)
            assignment = shared.record(ctx.membership.school, c.DRIVER_ASSIGNMENT, driver_membership_id)
            if not assignment or not assignment.get("active"):
                raise Rejected("driverMembershipId must be a real, currently assigned Driver.")
            route_id = assignment["routeId"]

        thread_id = f"driver-thread-{driver_membership_id}"
        if text(p, "threadId", max_len=160) != thread_id:
            raise Rejected("threadId must match this Driver's own channel.")

        route = shared.record(ctx.membership.school, c.ROUTE, route_id) or {}
        return {
            "messageId": ctx.entity_id,
            "threadId": thread_id,
            "driverMembershipId": driver_membership_id,
            "routeId": route_id,
            "vehicle": route.get("vehicle", ""),
            "body": text(p, "body", max_len=_MAX_BODY),
            "createdAt": text(p, "createdAt", max_len=40, required=False),
            "receivedAt": ctx.now,
            "senderMembershipId": member_id,
            "senderRole": ctx.membership.role,
        }

    def visible(self, membership, payload):
        if membership.role in c.READERS:
            return payload
        if membership.role == "driver" and payload.get("driverMembershipId") == str(membership.id):
            return payload
        return None


class MessageReceiptHandler(EntityHandler):
    """A real 'I have seen this thread' receipt on the one real Driver <-> Transport Control
    channel - the Driver themselves, or Transport Control (the same `roles = {"driver"} | c.MANAGERS`
    shape `DriverMessageHandler` uses), may each record their own receipt. Entity id:
    `<membershipId>:thread-seen:<driver-thread-id>:<epoch>`, append-only."""

    entity_type = c.DRIVER_MESSAGE_RECEIPT
    roles = frozenset({"driver"}) | c.MANAGERS
    allow_delete = False

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.membership.role not in self.roles:
            raise Rejected("Your role may not mark this thread seen.")
        if ctx.operation != "create":
            raise Rejected("This record is never changed once recorded.")

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        member_id = str(ctx.membership.id)
        parts = ctx.entity_id.split(":")
        if len(parts) < 4 or parts[0] != member_id or parts[1] != "thread-seen" or not parts[-1].isdigit():
            raise Rejected("This receipt does not belong to the active sender.")
        thread_id = ":".join(parts[2:-1])
        if text(p, "threadId", max_len=160) != thread_id:
            raise Rejected("threadId must match the record.")

        if ctx.membership.role == "driver":
            driver_membership_id = member_id
        else:
            driver_membership_id = text(p, "driverMembershipId", max_len=64)
            assignment = shared.record(ctx.membership.school, c.DRIVER_ASSIGNMENT, driver_membership_id)
            if not assignment or not assignment.get("active"):
                raise Rejected("driverMembershipId must be a real, currently assigned Driver.")

        if thread_id != f"driver-thread-{driver_membership_id}":
            raise Rejected("threadId must match this Driver's own channel.")

        return {
            "id": ctx.entity_id,
            "threadId": thread_id,
            "driverMembershipId": driver_membership_id,
            "seenAt": text(p, "seenAt", max_len=40, required=False),
            "receivedAt": ctx.now,
            "membershipId": member_id,
        }

    def visible(self, membership, payload):
        if membership.role in c.READERS:
            return payload
        return payload if payload.get("membershipId") == str(membership.id) else None


class DriverAlertHandler(EntityHandler):
    """A real, school-wide operational notice from Transport Control to every real Driver -
    broadcast, not a per-driver thread: entity id LOCAL-<sender membershipId>-<epoch>, create-only,
    the same append-only shape every other channel in this module uses. `scopeLabel` is the
    sender's own free-text description of who this is really for ("All Routes", "Route 7 only") -
    informational only, not a second access-control layer: every real Driver in this school can
    read every real alert, the same broadcast shape a guardian announcement already uses on the
    other side of the app."""

    entity_type = c.DRIVER_ALERT
    roles = c.MANAGERS
    allow_delete = False

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.membership.role not in self.roles:
            raise Rejected("Your role may not send operational alerts.")
        if ctx.operation != "create":
            raise Rejected("This record is never changed once recorded.")

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        member_id = str(ctx.membership.id)
        prefix = f"LOCAL-{member_id}-"
        if not ctx.entity_id.startswith(prefix) or not ctx.entity_id[len(prefix):].isdigit():
            raise Rejected("This alert does not belong to the active sender.")

        priority = text(p, "priority", max_len=20)
        if priority not in c.DRIVER_ALERT_PRIORITIES:
            raise Rejected("priority must be one of: " + ", ".join(c.DRIVER_ALERT_PRIORITIES))

        return {
            "id": ctx.entity_id,
            "title": text(p, "title", max_len=_MAX_TITLE),
            "body": text(p, "body", max_len=_MAX_BODY),
            "priority": priority,
            "scopeLabel": text(p, "scopeLabel", max_len=_MAX_SCOPE_LABEL, required=False) or "All Routes",
            "createdAt": ctx.now,
            "senderMembershipId": member_id,
        }

    def visible(self, membership, payload):
        if membership.role in c.READERS or membership.role == "driver":
            return payload
        return None


class AlertReceiptHandler(_DriverAppendOnly):
    """membershipId:alert-read:<alertId>:<epoch> - a Driver's own real receipt for a real,
    currently existing operational alert. One-directional: nothing ever sent *to* a Driver here
    needs a receipt from anyone else."""

    entity_type = c.DRIVER_ALERT_RECEIPT

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        member_id = str(ctx.membership.id)
        parts = ctx.entity_id.split(":")
        if len(parts) < 4 or parts[0] != member_id or parts[1] != "alert-read" or not parts[-1].isdigit():
            raise Rejected("This receipt does not belong to the active Driver.")
        alert_id = ":".join(parts[2:-1])
        if text(p, "alertId", max_len=160) != alert_id:
            raise Rejected("alertId must match the record.")
        if not shared.record(ctx.membership.school, c.DRIVER_ALERT, alert_id):
            raise Rejected("alertId must be a real operational alert.")

        route_id = daily_run.require_driver_route(ctx)
        if text(p, "routeId", max_len=64) != route_id:
            raise Rejected("routeId must match the Driver's real current assignment.")
        return {
            "id": ctx.entity_id,
            "alertId": alert_id,
            "routeId": route_id,
            "readAt": text(p, "readAt", max_len=40, required=False),
            "receivedAt": ctx.now,
            "membershipId": member_id,
        }
