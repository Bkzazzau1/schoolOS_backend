from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.organizations.services import serialize_organization_membership

from .onboarding import register_proprietor_account


class ProprietorRegisterView(APIView):
    """Create a new SchoolOS account and its first organization.

    This endpoint is intentionally public and rate-limited. It does not create a
    school tenant; the signed-in owner proceeds to Account Home and provisions
    the first school through the normal organization endpoint.
    """

    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "registration"

    def post(self, request):
        user, organization, organization_membership = register_proprietor_account(
            first_name=request.data.get("firstName"),
            last_name=request.data.get("lastName"),
            email=request.data.get("email"),
            password=request.data.get("password"),
            organization_name=request.data.get("organizationName"),
        )

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
            },
            status=status.HTTP_201_CREATED,
        )
