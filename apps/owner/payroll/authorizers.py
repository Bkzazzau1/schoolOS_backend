from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import string_list, text
from apps.schools.models import Role
from apps.sync.registry import EntityHandler, MutationContext

from ..common import REVOKED, resolve_status, server_owned_link
from .constants import AUTHORITIES


class AuthorizerHandler(EntityHandler):
    """Who the owner has authorized to prepare, approve or release payroll, or
    to approve new staff.

    This is what gives a person real power, so the app cannot make one active
    or link it to an account itself: `status: active` and `membershipId` are
    set only by the server when the person's account is linked. Records cannot
    be deleted, only revoked, so the trail of who was authorized stays.
    """

    entity_type = "owner_payroll_authorizer"
    roles = frozenset({Role.PROPRIETOR})

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        existing = ctx.existing or {}
        if text(p, "staffId", max_len=128) != ctx.entity_id:
            raise Rejected("staffId must match the record.")
        status = resolve_status(p, ctx.existing)
        cleaned = {
            "staffId": ctx.entity_id,
            "name": text(p, "name"),
            "authorities": string_list(p, "authorities", allowed=AUTHORITIES),
            "status": status,
            "membershipId": server_owned_link(ctx.existing),
            "grantedByMembershipId": str(ctx.membership.id),
            "createdAt": existing.get("createdAt") or ctx.now,
            "updatedAt": ctx.now,
        }
        if status == REVOKED:
            cleaned["revokedByMembershipId"] = str(ctx.membership.id)
        return cleaned
