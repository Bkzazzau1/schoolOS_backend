"""Who may do what with the school's bank accounts.

Connecting, rotating and disconnecting an account is the owner's, or someone the owner has
explicitly given the `finance.bank_connections` duty - being in the Finance Office is not
enough on its own. Looking at collections and working the review queue is the owner and the
Finance Office. Nobody else, whatever their role, and never across schools.
"""

from uuid import UUID

from rest_framework.exceptions import PermissionDenied

from apps.core.permissions import require_membership
from apps.owner.jobs.access import JOB, has_duty
from apps.schools.models import Membership, Role
from apps.sync.models import SyncRecord

from .constants import DUTY_MANAGE_CONNECTIONS


def can_manage_connections(membership) -> bool:
    return bool(membership.is_active) and (
        membership.role == Role.PROPRIETOR or has_duty(membership, DUTY_MANAGE_CONNECTIONS)
    )


def can_view_collections(membership) -> bool:
    return bool(membership.is_active) and (
        membership.role in (Role.PROPRIETOR, Role.ACCOUNTANT) or has_duty(membership, DUTY_MANAGE_CONNECTIONS)
    )


def collections_recipients(school) -> list[Membership]:
    """Everyone at the school who may look at collections: the owner, the finance office, and anyone
    the owner gave the bank-connections duty. These are the people told when money arrives."""
    holders = set()
    for row in SyncRecord.objects.filter(school=school, entity_type=JOB, deleted=False):
        if row.payload.get("status") == "active" and DUTY_MANAGE_CONNECTIONS in (row.payload.get("duties") or []):
            try:
                holders.add(UUID(str(row.payload.get("membershipId"))))
            except ValueError:
                continue
    people = Membership.objects.select_related("school").filter(school=school, is_active=True)
    return [m for m in people if m.role in (Role.PROPRIETOR, Role.ACCOUNTANT) or m.id in holders]


def _named_membership(request):
    query = getattr(request, "query_params", {})
    value = query.get("membership")
    data = getattr(request, "data", None)
    if not value and hasattr(data, "get"):
        value = data.get("membership")
    return value or None


def acting_membership(request, school_id, *, manage: bool = False):
    """The membership the person is acting as, checked for this school and this level of access."""
    membership = require_membership(request.user, school_id, membership_id=_named_membership(request))
    allowed = can_manage_connections(membership) if manage else can_view_collections(membership)
    if not allowed:
        raise PermissionDenied(
            "Only the owner, or someone the owner has authorised, can manage the school's bank accounts."
            if manage
            else "Collections are for the owner and the Finance Office."
        )
    return membership
