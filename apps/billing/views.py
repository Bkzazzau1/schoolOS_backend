from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.models import OrganizationMembership

from .models import Plan
from .services import ensure_organization_subscription, serialize_plan, serialize_subscription


def _organization_membership(user, organization_id) -> OrganizationMembership:
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


class PlanListView(APIView):
    """Read-only public plan catalog for signed-in SchoolOS accounts."""

    def get(self, request):
        plans = (
            Plan.objects.filter(is_active=True, is_public=True)
            .prefetch_related("entitlements")
            .order_by("sort_order", "name")
        )
        return Response(
            {
                "plans": [
                    serialize_plan(plan, include_entitlements=True)
                    for plan in plans
                ]
            }
        )


class OrganizationSubscriptionView(APIView):
    """Current commercial state for one organization the person belongs to."""

    def get(self, request, organization_id):
        membership = _organization_membership(request.user, organization_id)
        subscription = ensure_organization_subscription(membership.organization)
        subscription = (
            type(subscription).objects.select_related("organization", "plan")
            .prefetch_related("plan__entitlements")
            .get(pk=subscription.pk)
        )
        return Response(
            serialize_subscription(
                subscription,
                membership_role=membership.role,
            )
        )
