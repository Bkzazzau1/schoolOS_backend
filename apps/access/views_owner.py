"""The owner's screens for deciding who sees what. Every view is owner-only."""

from functools import wraps

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.schools.models import Membership, Role

from . import catalog, services
from .models import AccessChange
from .permissions import require_owner
from .serializers import OverrideSerializer, ReassignSerializer, RoleDefaultsSerializer
from .services import AccessError


def _refuses(view):
    """Turn a refused change into a 400 the app can show."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except AccessError as error:
            return Response({"code": "access_error", "message": error.message}, status=status.HTTP_400_BAD_REQUEST)

    return wrapper


def _override_json(o):
    now = timezone.now()
    if o.expires_at is not None and o.expires_at <= now:
        state = "expired"
    elif o.is_pending(now):
        state = "waiting_for_sync"  # told, but still in use until they sync or the time passes
    else:
        state = "in_force"
    return {
        "activity": o.activity,
        "effect": o.effect,
        "state": state,
        "expiresAt": o.expires_at.isoformat() if o.expires_at else None,
        "takesEffectBy": o.finalize_at.isoformat() if state == "waiting_for_sync" else None,
        "note": o.note,
        "setAt": o.set_at.isoformat(),
    }


class CatalogView(APIView):
    """Every activity, grouped, with who has it by default and in this school."""

    def get(self, request, school_id):
        owner = require_owner(request.user, school_id)
        by_role = {role: services.role_defaults(owner.school, role) for role in Role.values}
        by_role[Role.PROPRIETOR.value] = catalog.default_keys(Role.PROPRIETOR.value)
        return Response(
            {
                "groups": [
                    {
                        "area": group[0].area,
                        "activities": [
                            {
                                "key": a.key, "label": a.label,
                                "essential": a.essential, "grantable": a.grantable, "sensitive": a.sensitive,
                                "defaultRoles": sorted(a.default_roles),
                                "rolesInThisSchool": sorted(r for r, keys in by_role.items() if a.key in keys),
                            }
                            for a in group
                        ],
                    }
                    for group in catalog.GROUPS
                ]
            }
        )


class RolesView(APIView):
    """What each role gets in this school, and whether the owner changed it."""

    def get(self, request, school_id):
        owner = require_owner(request.user, school_id)
        changed = set(owner.school.role_activities.values_list("role", flat=True))
        return Response(
            {
                "roles": [
                    {
                        "role": role,
                        "activities": sorted(services.role_defaults(owner.school, role)),
                        "customized": role in changed,
                        "editable": role != Role.PROPRIETOR,
                    }
                    for role in Role.values
                    if role != Role.PROPRIETOR
                ]
            }
        )


class RoleDetailView(APIView):
    """PUT sets a role's activities in this school. DELETE goes back to the built-in ones."""

    @_refuses
    def put(self, request, school_id, role):
        owner = require_owner(request.user, school_id)
        body = RoleDefaultsSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        keys = services.set_role_defaults(owner, role, set(body.validated_data["activities"]))
        return Response({"role": role, "activities": sorted(keys)})

    @_refuses
    def delete(self, request, school_id, role):
        owner = require_owner(request.user, school_id)
        return Response({"role": role, "activities": sorted(services.reset_role_defaults(owner, role))})


class PeopleView(APIView):
    """Everyone in the school, what they can see, and what was granted or blocked."""

    def get(self, request, school_id):
        owner = require_owner(request.user, school_id)
        members = (
            Membership.objects.filter(school=owner.school, is_active=True)
            .select_related("user")
            .prefetch_related("activity_overrides")
            .order_by("user__email", "role")
        )
        return Response(
            {
                "people": [
                    {
                        "membershipId": str(m.id),
                        "email": m.user.email,
                        "name": m.user.get_full_name(),
                        "role": m.role,
                        "activities": sorted(services.effective_activities(m)),
                        "overrides": [_override_json(o) for o in m.activity_overrides.all()],
                    }
                    for m in members
                ]
            }
        )


class PersonActivityView(APIView):
    """PUT grants or blocks one activity for one person. DELETE restores their role's default."""

    @_refuses
    def put(self, request, school_id, membership_id, activity):
        owner = require_owner(request.user, school_id)
        body = OverrideSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        row = services.set_person_override(
            owner, membership_id, activity, body.validated_data["effect"],
            expires_at=body.validated_data.get("expiresAt"),
            note=body.validated_data.get("note", ""),
            mode=body.validated_data["mode"],
        )
        return Response(_override_json(row))

    @_refuses
    def delete(self, request, school_id, membership_id, activity):
        owner = require_owner(request.user, school_id)
        if not services.clear_person_override(owner, membership_id, activity):
            return Response({"code": "no_override", "message": "Nothing was set for this person."}, status=404)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ReassignView(APIView):
    """POST moves an activity from one person to another in one step."""

    @_refuses
    def post(self, request, school_id):
        owner = require_owner(request.user, school_id)
        body = ReassignSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        services.reassign(
            owner, data["activity"], data["fromMembershipId"], data["toMembershipId"],
            note=data.get("note", ""), mode=data["mode"],
        )
        return Response({"activity": data["activity"], "from": str(data["fromMembershipId"]), "to": str(data["toMembershipId"])})


class AuditView(APIView):
    """Who changed access, newest first."""

    def get(self, request, school_id):
        owner = require_owner(request.user, school_id)
        try:
            limit = min(max(int(request.query_params.get("limit", 50)), 1), 200)
        except ValueError:
            limit = 50
        changes = (
            AccessChange.objects.filter(school=owner.school)
            .select_related("actor__user", "target__user")[:limit]
        )
        return Response(
            {
                "changes": [
                    {
                        "at": c.at.isoformat(), "kind": c.kind, "activity": c.activity, "role": c.role,
                        "by": c.actor.user.email if c.actor else None,
                        "for": c.target.user.email if c.target else None,
                        "detail": c.detail,
                    }
                    for c in changes
                ]
            }
        )
