from functools import wraps

from django.db import models
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import require_activity
from apps.schools.models import Role

from .models import AlumniEvent, AlumniEventRsvp, AlumniProfile, AlumniVerificationStatus
from .serializers import (
    AlumniDirectoryEntrySerializer,
    AlumniEventCreateSerializer,
    AlumniEventRsvpSerializer,
    AlumniEventSerializer,
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


def _self_membership(request, school_id, activity="alumni.profile"):
    membership = require_activity(
        request.user,
        school_id,
        activity,
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


class AlumniDirectoryView(APIView):
    """Every real, verified alumnus who has chosen to be directory-visible - a deliberately narrow,
    public-facing subset of their profile (never admission number, original student reference or
    email). Any real Alumni membership of this school may browse it."""

    def get(self, request, school_id):
        _self_membership(request, school_id, activity="alumni.directory")
        profiles = (
            AlumniProfile.objects.filter(
                school_id=school_id,
                verification_status=AlumniVerificationStatus.VERIFIED,
                directory_visible=True,
            )
            .select_related("membership__user")
            .order_by("-graduation_year", "membership__user__email")
        )

        query = request.query_params.get("q", "").strip()
        if query:
            profiles = profiles.filter(
                models.Q(membership__user__first_name__icontains=query)
                | models.Q(membership__user__last_name__icontains=query)
                | models.Q(profession__icontains=query)
                | models.Q(organisation__icontains=query)
            )

        graduation_year = request.query_params.get("graduationYear", "").strip()
        if graduation_year.isdigit():
            profiles = profiles.filter(graduation_year=int(graduation_year))

        return Response(
            {"entries": [AlumniDirectoryEntrySerializer(profile).data for profile in profiles]}
        )


class AlumniEventListView(APIView):
    """Real reunion/event records for this school. Any real Alumni membership may read and see
    their own real RSVP state; only school management may create one - alumni browse and RSVP,
    they do not propose their own events."""

    def get(self, request, school_id):
        membership = _self_membership(request, school_id, activity="alumni.events")
        events = (
            AlumniEvent.objects.filter(school_id=school_id)
            .prefetch_related("rsvps")
        )
        return Response(
            {
                "events": [
                    AlumniEventSerializer(event, context={"viewer": membership}).data
                    for event in events
                ]
            }
        )

    @_alumni_error
    def post(self, request, school_id):
        manager = require_alumni_manager(request, school_id)
        body = AlumniEventCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        event = AlumniEvent.objects.create(
            school=manager.school,
            created_by=manager,
            title=body.validated_data["title"],
            date=body.validated_data["date"],
            time_text=body.validated_data.get("timeText", ""),
            venue=body.validated_data.get("venue", ""),
            note=body.validated_data.get("note", ""),
        )
        return Response(
            {"event": AlumniEventSerializer(event, context={"viewer": None}).data},
            status=status.HTTP_201_CREATED,
        )


class AlumniEventRsvpView(APIView):
    def post(self, request, school_id, event_id):
        membership = _self_membership(request, school_id, activity="alumni.events")
        event = get_object_or_404(AlumniEvent, id=event_id, school_id=school_id)
        body = AlumniEventRsvpSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        AlumniEventRsvp.objects.update_or_create(
            event=event,
            membership=membership,
            defaults={"attending": body.validated_data["attending"]},
        )
        return Response(
            {"event": AlumniEventSerializer(event, context={"viewer": membership}).data}
        )


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
