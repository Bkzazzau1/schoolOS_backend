from functools import wraps

from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import require_activity
from apps.schools.models import Role

from .models import AlumniProfile, AlumniVerificationStatus
from .serializers import (
    AlumniProfileSerializer,
    AlumniRejectSerializer,
    AlumniReviewSerializer,
    AlumniSelfProfileWriteSerializer,
    AlumniTransitionSerializer,
)
from .services import (
    AlumniError,
    reject_profile,
    require_alumni_manager,
    save_self_profile,
    transition_candidates,
    transition_student,
    verify_profile,
)


def _alumni_error(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except AlumniError as error:
            return Response(
                {"code": "alumni_error", "message": error.message},
                status=status.HTTP_400_BAD_REQUEST,
            )

    return wrapped


def _self_membership(request, school_id):
    membership = require_activity(
        request.user,
        school_id,
        "alumni.profile",
        membership_id=request.query_params.get("membership"),
    )
    if membership.role != Role.ALUMNI:
        raise PermissionDenied("This endpoint requires an Alumni membership.")
    return membership


class MyAlumniProfileView(APIView):
    def get(self, request, school_id):
        membership = _self_membership(request, school_id)
        profile = (
            AlumniProfile.objects.filter(
                school_id=school_id,
                membership=membership,
            )
            .select_related("membership__user", "verified_by")
            .first()
        )
        if profile is None:
            return Response(
                {
                    "membershipId": str(membership.id),
                    "schoolId": str(membership.school_id),
                    "role": membership.role,
                    "profile": None,
                    "message": "Your alumni profile has not been submitted yet.",
                }
            )
        return Response({"profile": AlumniProfileSerializer(profile).data})

    @_alumni_error
    def put(self, request, school_id):
        return self._save(request, school_id)

    @_alumni_error
    def patch(self, request, school_id):
        return self._save(request, school_id)

    def _save(self, request, school_id):
        membership = _self_membership(request, school_id)
        body = AlumniSelfProfileWriteSerializer(data=request.data, partial=True)
        body.is_valid(raise_exception=True)
        profile = save_self_profile(membership, body.validated_data)
        return Response({"profile": AlumniProfileSerializer(profile).data})


class AlumniManagementView(APIView):
    def get(self, request, school_id):
        manager = require_alumni_manager(request, school_id)
        wanted = request.query_params.get("status", "all").strip().lower()
        profiles = (
            AlumniProfile.objects.filter(school=manager.school)
            .select_related("membership__user", "verified_by__user")
            .order_by("verification_status", "-submitted_at", "membership__user__email")
        )
        if wanted in AlumniVerificationStatus.values:
            profiles = profiles.filter(verification_status=wanted)

        candidates = transition_candidates(manager.school)
        return Response(
            {
                "profiles": [AlumniProfileSerializer(profile).data for profile in profiles],
                "transitionCandidates": [
                    {
                        "membershipId": str(candidate.id),
                        "email": candidate.user.email,
                        "name": candidate.user.get_full_name() or candidate.user.email,
                    }
                    for candidate in candidates
                ],
            }
        )


class AlumniTransitionView(APIView):
    @_alumni_error
    def post(self, request, school_id):
        manager = require_alumni_manager(request, school_id)
        body = AlumniTransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        profile = transition_student(manager, body.validated_data)
        return Response(
            {"profile": AlumniProfileSerializer(profile).data},
            status=status.HTTP_201_CREATED,
        )


class AlumniVerifyView(APIView):
    @_alumni_error
    def post(self, request, school_id, alumni_membership_id):
        manager = require_alumni_manager(request, school_id)
        body = AlumniReviewSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        profile = verify_profile(
            manager,
            alumni_membership_id,
            body.validated_data.get("note", ""),
        )
        return Response({"profile": AlumniProfileSerializer(profile).data})


class AlumniRejectView(APIView):
    @_alumni_error
    def post(self, request, school_id, alumni_membership_id):
        manager = require_alumni_manager(request, school_id)
        body = AlumniRejectSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        profile = reject_profile(
            manager,
            alumni_membership_id,
            body.validated_data["note"],
        )
        return Response({"profile": AlumniProfileSerializer(profile).data})
