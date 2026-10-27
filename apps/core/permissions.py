from collections.abc import Iterable
from uuid import UUID

from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied

from apps.schools.models import Membership


def _enforce_subscription_access(membership: Membership, *, allow_when_restricted: bool) -> None:
    """A school whose organization's SkulGen subscription is restricted or suspended
    stops working for everyone at it, not just for account-growth actions
    (``apps.billing.services.require_entitlement`` already blocks those). A school
    created before the SaaS account layer, or one with no organization yet, is
    never affected.

    ``allow_when_restricted`` lets a caller that is read-only and safe to keep
    working during a milder "restricted" state (today: sync pull, so a school
    keeps seeing its own data) opt out of that half of the block; nothing opts
    out of "suspended" through this path - see ``bypass`` callers instead, used
    only by the handful of endpoints that must work regardless (reading one's
    own access state, so the app can explain why everything else is blocked).
    """
    school = membership.school
    if school.organization_id is None:
        return

    # Imported here, not at module load: apps.billing imports apps.core.permissions
    # (its own views resolve school memberships the same way everything else does),
    # so importing apps.billing.services at module level here would be circular.
    from apps.billing.services import access_mode, ensure_organization_subscription

    mode = access_mode(ensure_organization_subscription(school.organization))
    if mode == "full":
        return
    if mode == "account_restricted" and allow_when_restricted:
        return
    raise PermissionDenied(
        "Your school's SkulGen subscription is suspended. Contact your school's "
        "proprietor to resolve this."
        if mode == "suspended"
        else "Your school's SkulGen subscription needs attention: new changes are "
        "paused until it is resolved. Contact your school's proprietor."
    )


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


def active_membership(
    user,
    school_id,
    *,
    membership_id=None,
    roles: Iterable[str] | None = None,
    allow_when_restricted: bool = False,
    bypass_subscription_check: bool = False,
):
    """The person's active membership at that school, or None.

    Both the membership and the school must be active. Pass `membership_id` to
    require one specific membership (the app names the one it is acting as),
    and `roles` to require one of those roles. Without them, and if the person
    holds several roles here, this returns an arbitrary one, so callers that
    serve a person rather than a named membership should use `require_membership`.
    """
    query = Membership.objects.select_related("school__organization").filter(
        user=user, school_id=school_id, is_active=True, school__is_active=True
    )
    if membership_id is not None:
        query = query.filter(id=membership_id)
    if roles is not None:
        query = query.filter(role__in=list(roles))
    membership = query.first()
    if membership is not None and not bypass_subscription_check:
        _enforce_subscription_access(membership, allow_when_restricted=allow_when_restricted)
    return membership


def require_membership(
    user,
    school_id,
    *,
    roles: Iterable[str] | None = None,
    membership_id=None,
    allow_when_restricted: bool = False,
    bypass_subscription_check: bool = False,
):
    """The membership the person is acting as, or an error.

    - A missing one is a 403.
    - If `membership_id` is given, it must be the person's own.
    - If it is not given and the person holds several matching roles here, that
      is a 400 `membership_required` listing them, never a silent guess.
    - A school whose organization's subscription is restricted or suspended is
      blocked here too (see `_enforce_subscription_access`), unless the caller
      opts out with `allow_when_restricted`/`bypass_subscription_check`.
    """
    if membership_id is not None:
        try:
            membership_id = UUID(str(membership_id))
        except ValueError:
            raise PermissionDenied("You do not have access to this school.")
    query = Membership.objects.select_related("school__organization").filter(
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
    membership = found[0]
    if not bypass_subscription_check:
        _enforce_subscription_access(membership, allow_when_restricted=allow_when_restricted)
    return membership


def membership_from_request(
    request,
    school_id,
    *,
    allow_when_restricted: bool = False,
    bypass_subscription_check: bool = False,
):
    """The membership a request is for: `?membership=<id>` if given, otherwise
    the person's only one at this school."""
    return require_membership(
        request.user,
        school_id,
        membership_id=request.query_params.get("membership"),
        allow_when_restricted=allow_when_restricted,
        bypass_subscription_check=bypass_subscription_check,
    )
