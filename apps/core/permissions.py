from collections.abc import Iterable

from rest_framework.exceptions import PermissionDenied

from apps.schools.models import Membership


def active_membership(user, school_id, *, membership_id=None, roles: Iterable[str] | None = None):
    """The person's active membership at that school, or None.

    Both the membership and the school must be active. Pass `membership_id` to
    require one specific membership (the app names the one it is acting as),
    and `roles` to require one of those roles.
    """
    query = Membership.objects.select_related("school").filter(
        user=user, school_id=school_id, is_active=True, school__is_active=True
    )
    if membership_id is not None:
        query = query.filter(id=membership_id)
    if roles is not None:
        query = query.filter(role__in=list(roles))
    return query.first()


def require_membership(user, school_id, *, roles: Iterable[str] | None = None):
    """Like active_membership, but a missing one is a 403 for the caller."""
    membership = active_membership(user, school_id, roles=roles)
    if membership is None:
        raise PermissionDenied("You do not have access to this school.")
    return membership
