"""Who may do what with the school's bank accounts.

Connecting, rotating and disconnecting an account is the owner's, or someone the owner has
explicitly given the `finance.bank_connections` duty - being in the Finance Office is not
enough on its own. Looking at collections and working the review queue is the owner and the
Finance Office. Nobody else, whatever their role, and never across schools.
"""

from rest_framework.exceptions import PermissionDenied

from apps.core.permissions import require_membership
from apps.owner.jobs.access import has_duty
from apps.schools.models import Role

from .constants import DUTY_MANAGE_CONNECTIONS


def can_manage_connections(membership) -> bool:
    return bool(membership.is_active) and (
        membership.role == Role.PROPRIETOR or has_duty(membership, DUTY_MANAGE_CONNECTIONS)
    )


def can_view_collections(membership) -> bool:
    return bool(membership.is_active) and (
        membership.role in (Role.PROPRIETOR, Role.ACCOUNTANT) or has_duty(membership, DUTY_MANAGE_CONNECTIONS)
    )


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
