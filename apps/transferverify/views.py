from functools import wraps

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.errors import Rejected
from apps.core.permissions import require_membership
from apps.schools.models import Role

from . import associations as association_services
from . import disputes as dispute_services
from . import network as network_services
from . import verification as verification_services


def _transferverify_error(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except Rejected as error:
            return Response({"code": "transferverify_error", "message": error.message}, status=status.HTTP_400_BAD_REQUEST)

    return wrapped


class AssociationCatalogView(APIView):
    """The associations a Proprietor can browse to decide where to request
    joining - any signed-in person may read this catalog."""

    def get(self, request):
        return Response(
            {"associations": [association_services.serialize_association(a) for a in association_services.open_associations()]}
        )


class SchoolAssociationMembershipsView(APIView):
    """This school's own membership history across every association -
    owner only."""

    def get(self, request, school_id):
        membership = require_membership(
            request.user, school_id, roles=[Role.PROPRIETOR], membership_id=request.query_params.get("membership")
        )
        items = association_services.school_memberships(membership.school)
        return Response({"memberships": [association_services.serialize_school_membership(item) for item in items]})


class JoinAssociationView(APIView):
    @_transferverify_error
    def post(self, request, school_id, association_id):
        membership = require_membership(
            request.user, school_id, roles=[Role.PROPRIETOR], membership_id=request.data.get("membership")
        )
        item = association_services.request_to_join(membership=membership, association_id=association_id)
        return Response(
            {"membership": association_services.serialize_school_membership(item)}, status=status.HTTP_201_CREATED
        )


class ExitAssociationMembershipView(APIView):
    @_transferverify_error
    def post(self, request, school_id, membership_id):
        membership = require_membership(
            request.user, school_id, roles=[Role.PROPRIETOR], membership_id=request.data.get("membership")
        )
        item = association_services.exit_membership(membership=membership, membership_id=membership_id)
        return Response({"membership": association_services.serialize_school_membership(item)})


class AssociationMembersView(APIView):
    """An association administrator's view of the schools that have asked to
    join, or already belong to, their association."""

    @_transferverify_error
    def get(self, request, association_id):
        wanted = request.query_params.get("status")
        items = association_services.association_members(user=request.user, association_id=association_id, status=wanted)
        return Response({"members": [association_services.serialize_school_membership(item) for item in items]})


class ApproveAssociationMemberView(APIView):
    @_transferverify_error
    def post(self, request, association_id, membership_id):
        item = association_services.approve_membership(
            user=request.user, association_id=association_id, membership_id=membership_id, note=request.data.get("note", "")
        )
        return Response({"membership": association_services.serialize_school_membership(item)})


class RejectAssociationMemberView(APIView):
    @_transferverify_error
    def post(self, request, association_id, membership_id):
        item = association_services.reject_membership(
            user=request.user, association_id=association_id, membership_id=membership_id, note=request.data.get("note", "")
        )
        return Response({"membership": association_services.serialize_school_membership(item)})


class SuspendAssociationMemberView(APIView):
    @_transferverify_error
    def post(self, request, association_id, membership_id):
        item = association_services.suspend_membership(
            user=request.user, association_id=association_id, membership_id=membership_id, note=request.data.get("note", "")
        )
        return Response({"membership": association_services.serialize_school_membership(item)})


class NetworkPhoneMatchView(APIView):
    """A candidate-only guardian-phone lookup, meant to be called from
    within an admission's own identity-check step - never a general,
    unrestricted cross-school student search (see the docstring on
    apps.transferverify.network.match_by_phone)."""

    @_transferverify_error
    def post(self, request, school_id):
        membership = require_membership(
            request.user, school_id, roles=[Role.PROPRIETOR, Role.ADMINISTRATOR], membership_id=request.data.get("membership")
        )
        phone = request.data.get("phone")
        if not isinstance(phone, str):
            return Response({"code": "transferverify_error", "message": "phone is required."}, status=status.HTTP_400_BAD_REQUEST)
        candidates = network_services.match_by_phone(membership=membership, phone=phone)
        return Response({"candidates": candidates})


class SendVerificationRequestView(APIView):
    @_transferverify_error
    def post(self, request, school_id):
        membership = require_membership(
            request.user, school_id, roles=[Role.PROPRIETOR, Role.ADMINISTRATOR], membership_id=request.data.get("membership")
        )
        item = verification_services.send_request(
            membership=membership,
            transfer_alert_id=request.data.get("transferAlertId"),
            note=request.data.get("note", ""),
        )
        return Response({"request": verification_services.serialize_request(item)}, status=status.HTTP_201_CREATED)


class SentVerificationRequestsView(APIView):
    @_transferverify_error
    def get(self, request, school_id):
        membership = require_membership(
            request.user, school_id, roles=[Role.PROPRIETOR, Role.ADMINISTRATOR], membership_id=request.query_params.get("membership")
        )
        items = verification_services.requests_sent_by(membership.school)
        return Response({"requests": [verification_services.serialize_request(item) for item in items]})


class ReceivedVerificationRequestsView(APIView):
    @_transferverify_error
    def get(self, request, school_id):
        membership = require_membership(
            request.user, school_id, roles=[Role.PROPRIETOR], membership_id=request.query_params.get("membership")
        )
        items = verification_services.requests_received_by(membership.school)
        return Response({"requests": [verification_services.serialize_request(item) for item in items]})


class RespondVerificationRequestView(APIView):
    @_transferverify_error
    def post(self, request, school_id, request_id):
        membership = require_membership(
            request.user, school_id, roles=[Role.PROPRIETOR], membership_id=request.data.get("membership")
        )
        item = verification_services.respond_to_request(
            membership=membership,
            request_id=request_id,
            decision=request.data.get("decision"),
            note=request.data.get("note", ""),
        )
        return Response({"request": verification_services.serialize_request(item)})


class CancelVerificationRequestView(APIView):
    @_transferverify_error
    def post(self, request, school_id, request_id):
        membership = require_membership(
            request.user, school_id, roles=[Role.PROPRIETOR, Role.ADMINISTRATOR], membership_id=request.data.get("membership")
        )
        item = verification_services.cancel_request(membership=membership, request_id=request_id)
        return Response({"request": verification_services.serialize_request(item)})


class OpenDisputeView(APIView):
    """A guardian contesting a published case - see TransferClearanceDispute
    and apps.transferverify.disputes.open_dispute for the authority rule."""

    @_transferverify_error
    def post(self, request, school_id):
        membership = require_membership(
            request.user, school_id, roles=[Role.PARENT], membership_id=request.data.get("membership")
        )
        item = dispute_services.open_dispute(
            membership=membership,
            transfer_alert_id=request.data.get("transferAlertId"),
            reason=request.data.get("reason"),
            explanation=request.data.get("explanation", ""),
            evidence_references=request.data.get("evidenceReferences"),
        )
        return Response({"dispute": dispute_services.serialize_dispute(item)}, status=status.HTTP_201_CREATED)


class MyDisputesView(APIView):
    @_transferverify_error
    def get(self, request, school_id):
        membership = require_membership(
            request.user, school_id, roles=[Role.PARENT], membership_id=request.query_params.get("membership")
        )
        items = dispute_services.disputes_opened_by(membership)
        return Response({"disputes": [dispute_services.serialize_dispute(item) for item in items]})


class ReceivedDisputesView(APIView):
    @_transferverify_error
    def get(self, request, school_id):
        membership = require_membership(
            request.user, school_id, roles=[Role.PROPRIETOR], membership_id=request.query_params.get("membership")
        )
        items = dispute_services.disputes_for_school(membership.school)
        return Response({"disputes": [dispute_services.serialize_dispute(item) for item in items]})


class ReviewDisputeView(APIView):
    @_transferverify_error
    def post(self, request, school_id, dispute_id):
        membership = require_membership(
            request.user, school_id, roles=[Role.PROPRIETOR], membership_id=request.data.get("membership")
        )
        item = dispute_services.review_dispute(
            membership=membership, dispute_id=dispute_id,
            decision=request.data.get("decision"), note=request.data.get("note", ""),
        )
        return Response({"dispute": dispute_services.serialize_dispute(item)})


class IssueClearanceView(APIView):
    @_transferverify_error
    def post(self, request, school_id):
        membership = require_membership(
            request.user, school_id, roles=[Role.PROPRIETOR], membership_id=request.data.get("membership")
        )
        item = dispute_services.issue_clearance(
            membership=membership, external_id=request.data.get("externalId"), note=request.data.get("note", "")
        )
        return Response({"clearance": dispute_services.serialize_clearance(item)}, status=status.HTTP_201_CREATED)


class SchoolClearancesView(APIView):
    @_transferverify_error
    def get(self, request, school_id):
        membership = require_membership(
            request.user, school_id, roles=[Role.PROPRIETOR], membership_id=request.query_params.get("membership")
        )
        items = dispute_services.clearances_for_school(membership.school)
        return Response({"clearances": [dispute_services.serialize_clearance(item) for item in items]})


class RevokeClearanceView(APIView):
    @_transferverify_error
    def post(self, request, school_id, clearance_id):
        membership = require_membership(
            request.user, school_id, roles=[Role.PROPRIETOR], membership_id=request.data.get("membership")
        )
        item = dispute_services.revoke_clearance(membership=membership, clearance_id=clearance_id)
        return Response({"clearance": dispute_services.serialize_clearance(item)})


class VerifyClearanceView(APIView):
    """Public, unauthenticated - see apps.transferverify.disputes.verify_clearance
    for exactly why the response is this minimal."""

    authentication_classes = ()
    permission_classes = (AllowAny,)

    def get(self, request):
        result = dispute_services.verify_clearance(request.query_params.get("token", ""))
        return Response(result)


class MyCaseStatusView(APIView):
    """A guardian's own read-only view of any published case this school
    has against their own recorded child - see
    apps.transferverify.disputes.case_status_for_guardian."""

    @_transferverify_error
    def get(self, request, school_id):
        membership = require_membership(
            request.user, school_id, roles=[Role.PARENT], membership_id=request.query_params.get("membership")
        )
        items = dispute_services.case_status_for_guardian(membership)
        return Response({"cases": items})
