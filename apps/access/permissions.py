"""How other features check access.

    from apps.access.permissions import require_activity

    def get(self, request, school_id):
        require_activity(request.user, school_id, "finance.payroll")

Hiding a menu item in the app is a convenience. This is the real rule: a feature
must call it before returning or accepting data that belongs to an activity, so a
blocked person cannot reach it by calling the API directly.
"""

from rest_framework.exceptions import PermissionDenied

from apps.core.permissions import require_membership
from apps.schools.models import Role

from .services import has_activity


def require_owner(user, school_id):
    """The school's owner, or a 403."""
    return require_membership(user, school_id, roles=[Role.PROPRIETOR])


def require_activity(user, school_id, key: str, membership_id=None):
    """The person's membership if they may see `key`, otherwise a 403.

    Pass `membership_id` when the person may hold several roles here; without it,
    someone with more than one gets a 400 asking which.
    """
    membership = require_membership(user, school_id, membership_id=membership_id)
    if not has_activity(membership, key):
        raise PermissionDenied("This part of the app is not available to you.")
    return membership
