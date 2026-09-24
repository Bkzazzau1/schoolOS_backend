from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.models import OrganizationMembership

from .models import Membership, School


class MeView(APIView):
    """Who is signed in and which accounts/schools they can act in.

    School memberships remain the operational tenant authority. Organization
    memberships are account-level authority above those tenants and are returned
    separately so a proprietor can manage several schools without conflating
    account ownership with a role inside any one school.
    """

    def get(self, request):
        memberships = list(
            Membership.objects.filter(
                user=request.user,
                is_active=True,
                school__is_active=True,
            )
            .select_related("school", "school__organization")
            .order_by("school__name", "role")
        )
        organizations = list(
            OrganizationMembership.objects.filter(
                user=request.user,
                is_active=True,
                organization__is_active=True,
            )
            .select_related("organization")
            .order_by("organization__name", "role")
        )

        email_verified = request.user.email_verified_at is not None
        organization_ids = [membership.organization_id for membership in organizations]
        has_first_school = bool(organization_ids) and School.objects.filter(
            organization_id__in=organization_ids,
            is_active=True,
        ).exists()
        onboarding_applicable = bool(organizations)
        completed_count = (
            1 + int(email_verified) + int(has_first_school)
            if onboarding_applicable
            else 0
        )
        public_email = request.user.email
        if public_email.endswith("@accounts.schoolos.invalid"):
            public_email = ""

        return Response(
            {
                "id": str(request.user.id),
                "email": public_email,
                "name": request.user.get_full_name(),
                "mustChangePassword": bool(request.user.must_change_password),
                "emailVerified": email_verified,
                "onboarding": {
                    "applicable": onboarding_applicable,
                    "ready": (
                        email_verified and has_first_school
                        if onboarding_applicable
                        else True
                    ),
                    "completedCount": completed_count,
                    "totalCount": 3 if onboarding_applicable else 0,
                    "steps": (
                        [
                            {
                                "key": "account_created",
                                "label": "SchoolOS account created",
                                "completed": True,
                            },
                            {
                                "key": "email_verified",
                                "label": "Email address verified",
                                "completed": email_verified,
                            },
                            {
                                "key": "first_school_created",
                                "label": "First school created",
                                "completed": has_first_school,
                            },
                        ]
                        if onboarding_applicable
                        else []
                    ),
                },
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
