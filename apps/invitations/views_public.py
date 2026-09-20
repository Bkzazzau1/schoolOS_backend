from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from . import accept as accepting
from .service import InvitationError
from .throttles import AcceptThrottle, PreviewThrottle


def error_response(error: InvitationError) -> Response:
    return Response({"code": error.code, "message": error.message, **error.extra}, status=error.status)


class PreviewView(APIView):
    """GET invitations/<token>/  (no sign-in)

    What this link opens: the school, the person's name, a masked email, when it
    expires, and whether an account already exists for that email. Every bad link
    (unknown, expired, revoked, another school's) gives the same 404.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [PreviewThrottle]

    def get(self, request, token):
        try:
            return Response(
                accepting.preview(token, request_school=getattr(request, "school", None), request=request)
            )
        except InvitationError as error:
            return error_response(error)


class AcceptBody(serializers.Serializer):
    password = serializers.CharField(required=False, allow_blank=True, max_length=128, trim_whitespace=False)
    firstName = serializers.CharField(required=False, allow_blank=True, max_length=150)
    lastName = serializers.CharField(required=False, allow_blank=True, max_length=150)


class AcceptView(APIView):
    """POST invitations/<token>/accept/

    New person: send a password (and name). The account uses the invitation's email,
    never one from the request. Existing account: send that account's Bearer token
    instead. Returns sign-in tokens and the new membership.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AcceptThrottle]

    def post(self, request, token):
        body = AcceptBody(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        try:
            result = accepting.accept(
                token, signed_in_user=request.user, password=data.get("password", ""),
                first_name=data.get("firstName", ""), last_name=data.get("lastName", ""),
                request_school=getattr(request, "school", None), request=request,
            )
        except InvitationError as error:
            return error_response(error)
        refresh = RefreshToken.for_user(result["user"])
        membership = result["membership"]
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "membership": {
                    "id": str(membership.id), "schoolId": str(membership.school_id),
                    "schoolName": membership.school.name, "role": membership.role,
                },
                "staffId": result["staffId"],
            }
        )
