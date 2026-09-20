from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.errors import Rejected
from apps.core.permissions import membership_from_request

from . import identity, registration
from .proposals import service


def _refused(rejected: Rejected) -> Response:
    return Response({"code": "rejected", "message": rejected.message}, status=status.HTTP_400_BAD_REQUEST)


class ApproveBody(serializers.Serializer):
    # Only the owner may send these; anyone else sending them gets a clear refusal.
    gross = serializers.IntegerField(required=False)
    deductions = serializers.IntegerField(required=False)
    systemRole = serializers.CharField(required=False, max_length=20)


class RejectBody(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=200)


class ApproveProposalView(APIView):
    """POST staff/schools/<school>/proposals/<id>/approve/

    The owner, or someone the owner assigned to approve staff, makes the proposed
    person a staff member: directory entry, salary, profile and registration
    request, all at once or not at all. Repeating it is harmless.
    """

    def post(self, request, school_id, proposal_id):
        actor = membership_from_request(request, school_id)
        body = ApproveBody(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        try:
            result = service.approve(
                actor, proposal_id, gross=data.get("gross"), deductions=data.get("deductions"),
                system_role=data.get("systemRole"),
            )
        except Rejected as rejected:
            return _refused(rejected)
        return Response(result)


class RejectProposalView(APIView):
    """POST staff/schools/<school>/proposals/<id>/reject/  {"note": "..."}"""

    def post(self, request, school_id, proposal_id):
        actor = membership_from_request(request, school_id)
        body = RejectBody(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            service.reject(actor, proposal_id, body.validated_data.get("note", ""))
        except Rejected as rejected:
            return _refused(rejected)
        return Response({"ok": True})


class OnboardingBody(serializers.Serializer):
    personal = serializers.DictField()
    payment = serializers.DictField()
    documents = serializers.DictField(child=serializers.CharField(allow_blank=True, max_length=200), required=False)


def _own_request(request):
    """The registration waiting for the signed-in person, or None."""
    return registration.find_open_request(request.user, request.query_params.get("membership"))


class OnboardingView(APIView):
    """GET or POST staff/me/onboarding/

    The signed-in staff member's own registration: what has been filled in so far,
    and submitting it. It works only for the login linked to the staff record, and
    only while a request is open. Whichever way it is submitted (this, the web page,
    or the app's sync), the same rules apply.
    """

    def get(self, request):
        found = _own_request(request)
        if found is None:
            return Response(
                {"code": "no_open_request", "message": registration.NO_REQUEST}, status=status.HTTP_404_NOT_FOUND
            )
        membership, record = found
        p = record.payload
        return Response(
            {
                "staffId": record.entity_id,
                "schoolName": membership.school.name,
                "status": p.get("onboardingStatus"),
                "email": p.get("onboardingEmail", ""),
                "personal": p.get("personal", {}),
                "documents": [
                    {"name": d["name"], "status": d["status"]} for d in p.get("documents", [])
                ],
            }
        )

    def post(self, request):
        found = _own_request(request)
        if found is None:
            return Response(
                {"code": "no_open_request", "message": registration.NO_REQUEST}, status=status.HTTP_404_NOT_FOUND
            )
        body = OnboardingBody(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        try:
            staff_id = registration.submit(
                found[0], personal=data["personal"], payment=data["payment"], documents=data.get("documents")
            )
        except identity.Duplicate as duplicate:
            return Response({"code": "duplicate_identity", "message": duplicate.message}, status=status.HTTP_409_CONFLICT)
        except Rejected as rejected:
            return _refused(rejected)
        return Response({"staffId": staff_id, "status": "submitted"})
