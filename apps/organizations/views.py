from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.schools.models import School

from .models import OrganizationMembership
from .services import (
    create_organization,
    provision_school,
    serialize_organization_membership,
    serialize_school_membership,
)


def _organization_membership(user, organization_id):
    membership = (
        OrganizationMembership.objects.select_related("organization")
        .filter(
            user=user,
            organization_id=organization_id,
            is_active=True,
            organization__is_active=True,
        )
        .first()
    )
    if membership is None:
        raise PermissionDenied("You do not have access to this organization.")
    return membership


def _school_payload(school: School) -> dict:
    return {
        "id": str(school.id),
        "organizationId": str(school.organization_id) if school.organization_id else None,
        "name": school.name,
        "schoolType": school.school_type,
        "location": school.location,
        "isActive": school.is_active,
    }


class OrganizationListCreateView(APIView):
    """List the person's account-level memberships or create an organization."""

    def get(self, request):
        memberships = (
            OrganizationMembership.objects.select_related("organization")
            .filter(
                user=request.user,
                is_active=True,
                organization__is_active=True,
            )
            .order_by("organization__name", "role")
        )
        return Response(
            {"organizations": [serialize_organization_membership(m) for m in memberships]}
        )

    def post(self, request):
        name = request.data.get("name")
        if not isinstance(name, str):
            raise ValidationError({"message": "Enter an organization name."})
        organization, membership = create_organization(actor=request.user, name=name)
        return Response(
            {
                "organization": {
                    "id": str(organization.id),
                    "name": organization.name,
                    "slug": organization.slug,
                },
                "membership": serialize_organization_membership(membership),
            },
            status=status.HTTP_201_CREATED,
        )


class OrganizationSchoolsView(APIView):
    """Schools owned by an organization, and the atomic create-school command."""

    def get(self, request, organization_id):
        _organization_membership(request.user, organization_id)
        schools = School.objects.filter(
            organization_id=organization_id,
            is_active=True,
        ).order_by("name")
        return Response({"schools": [_school_payload(school) for school in schools]})

    def post(self, request, organization_id):
        name = request.data.get("name")
        school_type = request.data.get("schoolType")
        location = request.data.get("location")
        if not isinstance(name, str):
            raise ValidationError({"message": "Enter the school name."})
        if not isinstance(school_type, str):
            raise ValidationError({"message": "Choose a school type."})
        if not isinstance(location, str):
            raise ValidationError({"message": "Enter the school location."})

        school, membership = provision_school(
            actor=request.user,
            organization_id=organization_id,
            name=name,
            school_type=school_type,
            location=location,
        )
        return Response(
            {
                "school": _school_payload(school),
                "membership": serialize_school_membership(membership),
            },
            status=status.HTTP_201_CREATED,
        )
