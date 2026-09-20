from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.errors import Rejected
from apps.core.permissions import membership_from_request

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
