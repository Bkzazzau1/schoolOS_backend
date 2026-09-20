from collections.abc import Iterable
from uuid import UUID

from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied

from apps.schools.models import Membership


class MembershipRequired(APIException):
    """The person holds more than one role at this school (for example a teacher
    who is also a parent), and the request did not say which one it is for."""

    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, memberships):
        super().__init__(
            detail={
                "code": "membership_required",
                "message": "You have more than one role at this school. Say which one with ?membership=<id>.",
                "memberships": [{"id": str(m.id), "role": m.role} for m in memberships],
            }
        )


def active_membership(user, school_id, *, membership_id=None, roles: Iterable[str] | None = None):
    """The person's active membership at that school, or None.

    Both the membership and the school must be active. Pass `membership_id` to
    require one specific membership (the app names the one it is acting as),
    and `roles` to require one of those roles. Without them, and if the person
    holds several roles here, this returns an arbitrary one, so callers that
    serve a person rather than a named membership should use `require_membership`.
    """
    query = Membership.objects.select_related("school").filter(
        user=user, school_id=school_id, is_active=True, school__is_active=True
    )
    if membership_id is not None:
        query = query.filter(id=membership_id)
    if roles is not None:
        query = query.filter(role__in=list(roles))
    return query.first()


def require_membership(user, school_id, *, roles: Iterable[str] | None = None, membership_id=None):
    """The membership the person is acting as, or an error.

    - A missing one is a 403.
    - If `membership_id` is given, it must be the person's own.
    - If it is not given and the person holds several matching roles here, that
      is a 400 `membership_required` listing them, never a silent guess.
    """
    if membership_id is not None:
        try:
            membership_id = UUID(str(membership_id))
        except ValueError:
            raise PermissionDenied("You do not have access to this school.")
    query = Membership.objects.select_related("school").filter(
        user=user, school_id=school_id, is_active=True, school__is_active=True
    )
    if roles is not None:
        query = query.filter(role__in=list(roles))
    if membership_id is not None:
        query = query.filter(id=membership_id)
    found = list(query[:5])
    if not found:
        raise PermissionDenied("You do not have access to this school.")
    if len(found) > 1:
        raise MembershipRequired(found)
    return found[0]


def membership_from_request(request, school_id):
    """The membership a request is for: `?membership=<id>` if given, otherwise
    the person's only one at this school."""
    return require_membership(
        request.user, school_id, membership_id=request.query_params.get("membership")
    )
