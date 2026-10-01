from functools import wraps

from django.db import models
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import require_activity
from apps.schools.models import Role

from .models import (
    AlumniEvent,
    AlumniEventRsvp,
    AlumniMentorProfile,
    AlumniMentorshipRequest,
    AlumniMentorshipRequestStatus,
    AlumniOpportunity,
    AlumniOpportunityStatus,
    AlumniPledge,
    AlumniPledgeStatus,
    AlumniProfile,
    AlumniVerificationStatus,
)
from .serializers import (
    AlumniDirectoryEntrySerializer,
    AlumniEventCreateSerializer,
    AlumniEventRsvpSerializer,
    AlumniEventSerializer,
    AlumniMentorProfileSerializer,
    AlumniMentorProfileWriteSerializer,
    AlumniMentorshipRequestCreateSerializer,
    AlumniMentorshipRequestRespondSerializer,
    AlumniMentorshipRequestSerializer,
    AlumniOpportunityCreateSerializer,
    AlumniOpportunitySerializer,
    AlumniPledgeCreateSerializer,
    AlumniPledgeSerializer,
    AlumniPledgeStatusSerializer,
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


class AlumniPledgeListView(APIView):
    """A real alumnus's own non-monetary offers of help - never a public board of everyone's
    pledges, only the acting membership's own."""

    def get(self, request, school_id):
        membership = _self_membership(request, school_id, activity="alumni.give-back")
        pledges = AlumniPledge.objects.filter(school_id=school_id, membership=membership).select_related(
            "membership__user"
        )
        return Response({"pledges": [AlumniPledgeSerializer(pledge).data for pledge in pledges]})

    @_alumni_error
    def post(self, request, school_id):
        membership = _self_membership(request, school_id, activity="alumni.give-back")
        body = AlumniPledgeCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        pledge = AlumniPledge.objects.create(
            school=membership.school,
            membership=membership,
            category=body.validated_data["category"],
            description=body.validated_data["description"],
        )
        return Response(
            {"pledge": AlumniPledgeSerializer(pledge).data},
            status=status.HTTP_201_CREATED,
        )


class AlumniPledgeWithdrawView(APIView):
    @_alumni_error
    def post(self, request, school_id, pledge_id):
        membership = _self_membership(request, school_id, activity="alumni.give-back")
        pledge = get_object_or_404(AlumniPledge, id=pledge_id, school_id=school_id, membership=membership)
        if pledge.status not in (AlumniPledgeStatus.OFFERED, AlumniPledgeStatus.ACKNOWLEDGED):
            raise AlumniError("Only an offered or acknowledged pledge can be withdrawn.")
        pledge.status = AlumniPledgeStatus.WITHDRAWN
        pledge.save(update_fields=["status", "updated_at"])
        return Response({"pledge": AlumniPledgeSerializer(pledge).data})


class AlumniPledgeStatusView(APIView):
    @_alumni_error
    def post(self, request, school_id, pledge_id):
        manager = require_alumni_manager(request, school_id)
        pledge = get_object_or_404(AlumniPledge, id=pledge_id, school_id=school_id)
        body = AlumniPledgeStatusSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        pledge.status = body.validated_data["status"]
        if "schoolNote" in body.validated_data:
            pledge.school_note = body.validated_data["schoolNote"]
        pledge.save(update_fields=["status", "school_note", "updated_at"])
        return Response({"pledge": AlumniPledgeSerializer(pledge).data})


class AlumniOpportunityListView(APIView):
    """A real posting board: any real alumnus may post, and every real alumnus sees every real
    posting for their school - not just their own, unlike Give Back's self-only pledges."""

    def get(self, request, school_id):
        _self_membership(request, school_id, activity="alumni.opportunities")
        opportunities = AlumniOpportunity.objects.filter(school_id=school_id).select_related(
            "posted_by__user"
        )
        return Response(
            {"opportunities": [AlumniOpportunitySerializer(item).data for item in opportunities]}
        )

    @_alumni_error
    def post(self, request, school_id):
        membership = _self_membership(request, school_id, activity="alumni.opportunities")
        body = AlumniOpportunityCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        opportunity = AlumniOpportunity.objects.create(
            school=membership.school,
            posted_by=membership,
            title=body.validated_data["title"],
            organisation=body.validated_data["organisation"],
            opportunity_type=body.validated_data["opportunityType"],
            location_text=body.validated_data.get("locationText", ""),
            description=body.validated_data["description"],
            contact_info=body.validated_data.get("contactInfo", ""),
        )
        return Response(
            {"opportunity": AlumniOpportunitySerializer(opportunity).data},
            status=status.HTTP_201_CREATED,
        )


class AlumniOpportunityCloseView(APIView):
    """Closing follows the same 'author or moderator' shape `PostHandler.authorize` already
    established for Community posts - the real poster's own Alumni membership, or school
    management via `require_alumni_manager`, never anyone else's Alumni membership."""

    def post(self, request, school_id, opportunity_id):
        opportunity = get_object_or_404(AlumniOpportunity, id=opportunity_id, school_id=school_id)

        acting_alumnus = None
        try:
            acting_alumnus = _self_membership(request, school_id, activity="alumni.opportunities")
        except PermissionDenied:
            pass

        if acting_alumnus is not None:
            if acting_alumnus.id != opportunity.posted_by_id:
                raise PermissionDenied("You can only close your own posting.")
        else:
            require_alumni_manager(request, school_id)

        opportunity.status = AlumniOpportunityStatus.CLOSED
        opportunity.save(update_fields=["status", "updated_at"])
        return Response({"opportunity": AlumniOpportunitySerializer(opportunity).data})


class AlumniMentorDirectoryView(APIView):
    """Every real, active mentor profile for this school - never contact info, the same restraint
    `AlumniDirectoryEntrySerializer` already applies. Alumni mentoring alumni only; no manager role
    is involved anywhere in Mentorship."""

    def get(self, request, school_id):
        _self_membership(request, school_id, activity="alumni.mentorship")
        mentors = AlumniMentorProfile.objects.filter(school_id=school_id, is_active=True).select_related(
            "membership__user"
        )
        return Response({"mentors": [AlumniMentorProfileSerializer(mentor).data for mentor in mentors]})


class AlumniMyMentorProfileView(APIView):
    """A real alumnus's own opt-in mentor profile - a separate decision from the Alumni Directory,
    the same shape `MyAlumniProfileView` already uses for the identity profile."""

    def get(self, request, school_id):
        membership = _self_membership(request, school_id, activity="alumni.mentorship")
        profile = (
            AlumniMentorProfile.objects.filter(school_id=school_id, membership=membership)
            .select_related("membership__user")
            .first()
        )
        if profile is None:
            return Response({"mentorProfile": None})
        return Response({"mentorProfile": AlumniMentorProfileSerializer(profile).data})

    @_alumni_error
    def put(self, request, school_id):
        membership = _self_membership(request, school_id, activity="alumni.mentorship")
        body = AlumniMentorProfileWriteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        profile, _ = AlumniMentorProfile.objects.update_or_create(
            membership=membership,
            defaults={
                "school": membership.school,
                "expertise": body.validated_data["expertise"],
                "bio": body.validated_data["bio"],
                "is_active": body.validated_data.get("isActive", True),
            },
        )
        return Response({"mentorProfile": AlumniMentorProfileSerializer(profile).data})


class AlumniMentorshipRequestListView(APIView):
    """Every real request involving the acting alumnus, as mentor or as mentee - never a public
    board, the same self-scoped reasoning Give Back's pledges use."""

    def get(self, request, school_id):
        membership = _self_membership(request, school_id, activity="alumni.mentorship")
        requests = (
            AlumniMentorshipRequest.objects.filter(school_id=school_id)
            .filter(models.Q(mentor=membership) | models.Q(mentee=membership))
            .select_related("mentor__user", "mentee__user")
        )
        return Response(
            {"requests": [AlumniMentorshipRequestSerializer(item).data for item in requests]}
        )

    @_alumni_error
    def post(self, request, school_id):
        membership = _self_membership(request, school_id, activity="alumni.mentorship")
        body = AlumniMentorshipRequestCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        mentor_id = body.validated_data["mentorMembershipId"]

        if str(mentor_id) == str(membership.id):
            raise AlumniError("You cannot request yourself as a mentor.")
        mentor_profile = AlumniMentorProfile.objects.filter(
            school_id=school_id, membership_id=mentor_id, is_active=True
        ).first()
        if mentor_profile is None:
            raise AlumniError("Choose a real, currently active mentor.")
        if AlumniMentorshipRequest.objects.filter(
            school_id=school_id,
            mentor_id=mentor_id,
            mentee=membership,
            status=AlumniMentorshipRequestStatus.PENDING,
        ).exists():
            raise AlumniError("You already have a pending request to this mentor.")

        mentorship_request = AlumniMentorshipRequest.objects.create(
            school=membership.school,
            mentor_id=mentor_id,
            mentee=membership,
            message=body.validated_data.get("message", ""),
        )
        return Response(
            {"request": AlumniMentorshipRequestSerializer(mentorship_request).data},
            status=status.HTTP_201_CREATED,
        )


class AlumniMentorshipRequestRespondView(APIView):
    """Only the real mentor named on the request may accept or decline it."""

    @_alumni_error
    def post(self, request, school_id, request_id):
        membership = _self_membership(request, school_id, activity="alumni.mentorship")
        mentorship_request = get_object_or_404(
            AlumniMentorshipRequest, id=request_id, school_id=school_id, mentor=membership
        )
        if mentorship_request.status != AlumniMentorshipRequestStatus.PENDING:
            raise AlumniError("This request has already been answered.")
        body = AlumniMentorshipRequestRespondSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        mentorship_request.status = body.validated_data["status"]
        mentorship_request.save(update_fields=["status", "updated_at"])
        return Response({"request": AlumniMentorshipRequestSerializer(mentorship_request).data})


class AlumniMentorshipRequestWithdrawView(APIView):
    """Only the real mentee may withdraw their own request, and only while it is still pending -
    mirrors `AlumniPledgeWithdrawView`'s shape."""

    @_alumni_error
    def post(self, request, school_id, request_id):
        membership = _self_membership(request, school_id, activity="alumni.mentorship")
        mentorship_request = get_object_or_404(
            AlumniMentorshipRequest, id=request_id, school_id=school_id, mentee=membership
        )
        if mentorship_request.status != AlumniMentorshipRequestStatus.PENDING:
            raise AlumniError("Only a pending request can be withdrawn.")
        mentorship_request.status = AlumniMentorshipRequestStatus.WITHDRAWN
        mentorship_request.save(update_fields=["status", "updated_at"])
        return Response({"request": AlumniMentorshipRequestSerializer(mentorship_request).data})


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
        pledges = AlumniPledge.objects.filter(school=manager.school).select_related("membership__user")
        opportunities = AlumniOpportunity.objects.filter(school=manager.school).select_related(
            "posted_by__user"
        )
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
                "pledges": [AlumniPledgeSerializer(pledge).data for pledge in pledges],
                "opportunities": [AlumniOpportunitySerializer(item).data for item in opportunities],
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
