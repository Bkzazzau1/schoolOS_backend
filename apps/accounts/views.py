from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from apps.organizations.services import serialize_organization_membership

from .authentication import SchoolOSTokenSerializer, complete_initial_password
from .email_verification import confirm_email_verification, issue_email_verification
from .onboarding import register_proprietor_account
from .throttles import (
    EmailVerificationConfirmThrottle,
    EmailVerificationSendThrottle,
    LoginIdentifierThrottle,
    LoginNetworkThrottle,
    RegistrationThrottle,
)


class SchoolOSTokenView(APIView):
    """Sign in with email, student admission ID, or parent phone number."""

    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (LoginIdentifierThrottle, LoginNetworkThrottle)

    def post(self, request):
        serializer = SchoolOSTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data, status=status.HTTP_200_OK)


class InitialPasswordChangeView(APIView):
    """Replace the simple bootstrap password after first successful sign-in."""

    def post(self, request):
        complete_initial_password(request.user, request.data.get("newPassword"))
        return Response(
            {"changed": True, "mustChangePassword": False},
            status=status.HTTP_200_OK,
        )


class ProprietorRegisterView(APIView):
    """Create a new SkulGen account and its first organization.

    This endpoint is intentionally public and rate-limited. It does not create a
    school tenant; the signed-in owner proceeds to Account Home and provisions
    the first school through the normal organization endpoint.
    """

    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (RegistrationThrottle,)

    def post(self, request):
        user, organization, organization_membership = register_proprietor_account(
            first_name=request.data.get("firstName"),
            last_name=request.data.get("lastName"),
            email=request.data.get("email"),
            password=request.data.get("password"),
            organization_name=request.data.get("organizationName"),
        )

        # Mail delivery is best-effort. The account remains valid if SMTP is
        # temporarily unavailable; Account Home can request another code later.
        verification = issue_email_verification(user)

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "organization": {
                    "id": str(organization.id),
                    "name": organization.name,
                    "slug": organization.slug,
                },
                "organizationMembership": serialize_organization_membership(
                    organization_membership
                ),
                "emailVerification": verification,
            },
            status=status.HTTP_201_CREATED,
        )


class LogoutView(APIView):
    """Sign out of one session: this refresh token (and any access token
    derived from it, within its own short remaining lifetime) stops working.

    To sign out of every session at once instead, reset the password
    (apps.credentials.password_reset): that revokes immediately everywhere by
    incrementing credential_version, rather than waiting on one token's own
    lifetime.
    """

    def post(self, request):
        raw = request.data.get("refresh")
        if not isinstance(raw, str) or not raw:
            raise ValidationError({"refresh": "A refresh token is required."})
        try:
            token = RefreshToken(raw)
        except TokenError as exc:
            raise ValidationError({"refresh": "That refresh token is not valid."}) from exc
        if str(token.payload.get("user_id")) != str(request.user.id):
            raise ValidationError({"refresh": "That refresh token does not belong to this account."})
        try:
            token.blacklist()
        except TokenError as exc:
            raise ValidationError({"refresh": "That refresh token is not valid."}) from exc
        return Response({"loggedOut": True}, status=status.HTTP_200_OK)


class EmailVerificationSendView(APIView):
    """Issue a fresh verification code to the signed-in person's email."""

    throttle_classes = (EmailVerificationSendThrottle,)

    def post(self, request):
        return Response(
            issue_email_verification(request.user, enforce_cooldown=True),
            status=status.HTTP_200_OK,
        )


class EmailVerificationConfirmView(APIView):
    """Confirm the signed-in person's six-digit email verification code."""

    throttle_classes = (EmailVerificationConfirmThrottle,)

    def post(self, request):
        return Response(
            confirm_email_verification(request.user, request.data.get("code")),
            status=status.HTTP_200_OK,
        )
