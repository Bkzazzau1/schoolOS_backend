"""What a Driver sends and acknowledges from their own Messages screen: a message to school
operations, a "thread seen" receipt, an "alert read" receipt.

Only this direction exists: nothing in the app yet sends a message or an alert *to* a Driver
(driver_messages_snapshot, the read model that would carry them, has no writer anywhere), so
there is nothing on the server side to hand back. These records simply stop waiting forever on
a device and are there for the Transport Control inbox that will read them.
"""

from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import text
from apps.sync.registry import EntityHandler, MutationContext

from . import constants as c
from . import daily_run, shared

_MAX_BODY = 2000
_FORBIDDEN_CHANNEL_WORDS = ("parent", "guardian", "family")


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


class DriverMessageHandler(_DriverAppendOnly):
    """A message from a Driver to school operations (entity id: LOCAL-membershipId-epoch)."""

    entity_type = c.DRIVER_MESSAGE
    author_field = "senderMembershipId"

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        member_id = str(ctx.membership.id)
        prefix = f"LOCAL-{member_id}-"
        if not ctx.entity_id.startswith(prefix) or not ctx.entity_id[len(prefix):].isdigit():
            raise Rejected("This message does not belong to the active Driver.")
        if text(p, "messageId", max_len=160) != ctx.entity_id:
            raise Rejected("messageId must match the record.")

        route_id = daily_run.require_driver_route(ctx)
        if text(p, "routeId", max_len=64) != route_id:
            raise Rejected("routeId must match the Driver's real current assignment.")

        participant = text(p, "participantName", max_len=160)
        role = text(p, "participantRole", max_len=160)
        channel = text(p, "channelLabel", max_len=160)
        scope = f"{participant} {role} {channel}".lower()
        if any(word in scope for word in _FORBIDDEN_CHANNEL_WORDS):
            raise Rejected("Drivers cannot directly message parents or guardians from this workspace.")

        route = shared.record(ctx.membership.school, c.ROUTE, route_id) or {}
        return {
            "messageId": ctx.entity_id,
            "threadId": text(p, "threadId", max_len=160),
            "routeId": route_id,
            "vehicle": route.get("vehicle", ""),
            "participantName": participant,
            "participantRole": role,
            "channelLabel": channel,
            "body": text(p, "body", max_len=_MAX_BODY),
            "createdAt": text(p, "createdAt", max_len=40, required=False),
            "receivedAt": ctx.now,
            "senderMembershipId": member_id,
        }


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
