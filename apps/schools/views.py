from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.models import OrganizationMembership

from .models import Membership


class MeView(APIView):
    """Who is signed in and which accounts/schools they can act in.

    School memberships remain the operational tenant authority. Organization
    memberships are account-level authority above those tenants and are returned
    separately so a proprietor can manage several schools without conflating
    account ownership with a role inside any one school.
    """

    def get(self, request):
        memberships = (
            Membership.objects.filter(
                user=request.user,
                is_active=True,
                school__is_active=True,
            )
            .select_related("school", "school__organization")
            .order_by("school__name", "role")
        )
        organizations = (
            OrganizationMembership.objects.filter(
                user=request.user,
                is_active=True,
                organization__is_active=True,
            )
            .select_related("organization")
            .order_by("organization__name", "role")
        )
        return Response(
            {
                "id": str(request.user.id),
                "email": request.user.email,
                "name": request.user.get_full_name(),
                "memberships": [
                    {
                        "id": str(m.id),
                        "schoolId": str(m.school_id),
                        "schoolName": m.school.name,
                        "role": m.role,
                        "organizationId": (
                            str(m.school.organization_id)
                            if m.school.organization_id
                            else None
                        ),
                    }
                    for m in memberships
                ],
                "organizations": [
                    {
                        "id": str(m.id),
                        "organizationId": str(m.organization_id),
                        "organizationName": m.organization.name,
                        "role": m.role,
                    }
                    for m in organizations
                ],
            }
        )
