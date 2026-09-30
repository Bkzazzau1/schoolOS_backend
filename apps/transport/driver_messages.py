"""What passes between a Driver and Transport Control: a real, two-way message on their one real
shared channel, plus a Driver's own "thread seen" and "alert read" receipts.

The message channel is genuinely two-way: a Driver writes from their own Messages screen, and
Transport Control (apps/transport/constants.py MANAGERS - Proprietor or Administrator, the same
role set that already sets every other transport policy in this module) may write back into that
same Driver's thread, the same `roles = {"driver"} | c.MANAGERS` shape `incident.py` already uses
for a Driver-reported, management-reviewed record. Principal keeps its existing read-only
oversight (READERS). Receipts stay Driver-only and one-directional - a receipt is always the
Driver's own acknowledgement, and nothing read-receipted here is ever sent *to* a Driver by
anyone else.
"""

from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import text
from apps.sync.registry import EntityHandler, MutationContext

from . import constants as c
from . import daily_run, shared

_MAX_BODY = 2000


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


class _ReceiptHandler(_DriverAppendOnly):
    """membershipId:<kind>:<subjectId>:<epoch>, where the subject is a thread or an alert."""

    kind = ""  # "thread-seen" | "alert-read"
    subject_field = ""  # "threadId" | "alertId"
    time_field = ""  # "seenAt" | "readAt"

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        member_id = str(ctx.membership.id)
        parts = ctx.entity_id.split(":")
        if len(parts) < 4 or parts[0] != member_id or parts[1] != self.kind or not parts[-1].isdigit():
            raise Rejected("This receipt does not belong to the active Driver.")
        subject = ":".join(parts[2:-1])
        if text(p, self.subject_field, max_len=160) != subject:
            raise Rejected(f"{self.subject_field} must match the record.")
        if text(p, "id", max_len=300) != ctx.entity_id:
            raise Rejected("id must match the record.")

        route_id = daily_run.require_driver_route(ctx)
        if text(p, "routeId", max_len=64) != route_id:
            raise Rejected("routeId must match the Driver's real current assignment.")
        return {
            "id": ctx.entity_id,
            self.subject_field: subject,
            "routeId": route_id,
            self.time_field: text(p, self.time_field, max_len=40, required=False),
            "receivedAt": ctx.now,
            "membershipId": member_id,
        }


class MessageReceiptHandler(_ReceiptHandler):
    entity_type = c.DRIVER_MESSAGE_RECEIPT
    kind, subject_field, time_field = "thread-seen", "threadId", "seenAt"


class AlertReceiptHandler(_ReceiptHandler):
    entity_type = c.DRIVER_ALERT_RECEIPT
    kind, subject_field, time_field = "alert-read", "alertId", "readAt"
