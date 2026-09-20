from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import require_membership
from apps.staff.constants import DIRECTORY, INVITE_PENDING, INVITER_ROLES, PROFILE
from apps.sync import records

from . import service, unlink as unlinking
from .service import InvitationError
from .views_public import error_response


class ResendBody(serializers.Serializer):
    email = serializers.EmailField(required=False)


class StaffInvitationView(APIView):
    """owner/schools/<school>/staff/<staffId>/invitation/

    GET   where the invitation stands (never the link itself)  - owner, principal, administrator
    POST  send it again, optionally to a corrected email        - owner, principal, administrator
    DELETE cancel it                                            - owner only
    """

    def get(self, request, school_id, staff_id):
        member = require_membership(request.user, school_id, roles=sorted(INVITER_ROLES))
        return Response(service.describe(member.school, staff_id))

    def post(self, request, school_id, staff_id):
        member = require_membership(request.user, school_id, roles=sorted(INVITER_ROLES))
        school = member.school
        body = ResendBody(data=request.data)
        body.is_valid(raise_exception=True)
        profile = records.read(school, PROFILE, staff_id)
        if profile is None or records.read(school, DIRECTORY, staff_id) is None:
            return error_response(InvitationError("not_found", "That staff member was not found.", 404))
        email = (body.validated_data.get("email") or profile.get("onboardingEmail") or "").lower()
        try:
            if email != profile.get("onboardingEmail") or profile.get("onboardingStatus") != INVITE_PENDING:
                # A resend reopens the request, with the corrected email.
                records.write(school, PROFILE, staff_id,
                              {**profile, "onboardingEmail": email, "onboardingStatus": INVITE_PENDING}, by=member)
            service.create_invitation(school, staff_id, email, profile.get("systemRole") or "staff", created_by=member)
        except InvitationError as error:
            return error_response(error)
        return Response(service.describe(school, staff_id))

    def delete(self, request, school_id, staff_id):
        owner = require_membership(request.user, school_id, roles=["proprietor"])
        if not service.revoke(owner.school, staff_id, by=owner):
            return error_response(InvitationError("not_found", "There is no pending invitation.", 404))
        return Response(status=status.HTTP_204_NO_CONTENT)


class UnlinkView(APIView):
    """POST owner/schools/<school>/staff/<staffId>/unlink/  (owner only)"""

    def post(self, request, school_id, staff_id):
        owner = require_membership(request.user, school_id, roles=["proprietor"])
        try:
            unlinking.unlink(owner.school, staff_id, by=owner)
        except InvitationError as error:
            return error_response(error)
        return Response({"ok": True})
