"""Append-only: every morning/afternoon run and vehicle check milestone a Driver records
(started, arrived at a stop, a rider's status, submitted a check, ...). One shared entity
type across all three, the same way the app logs them - each event's own extra detail
varies by what happened, so it is kept flat rather than forced into one fixed shape."""

from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import text
from apps.sync.registry import EntityHandler, MutationContext

from . import constants as c
from . import shared


class TransportEventHandler(EntityHandler):
    entity_type = c.TRANSPORT_EVENT
    roles = frozenset({"driver"})

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation != "create":
            raise Rejected("A transport event is never changed once recorded.")
        super().authorize(ctx)

    def visible(self, membership, payload):
        if membership.role in c.READERS:
            return payload
        if membership.role == "driver" and payload.get("actorMembershipId") == str(membership.id):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        stored = {
            "id": ctx.entity_id,
            "eventType": text(p, "eventType", max_len=64),
            "at": ctx.now,
            "actorMembershipId": str(ctx.membership.id),
        }
        stored.update(shared.extra_event_detail(p, exclude=stored.keys()))
        return stored
