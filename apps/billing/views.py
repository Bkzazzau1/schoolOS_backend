from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.models import OrganizationMembership

from .models import BillingInvoice, PaymentAttempt, Plan
from .services import (
    can_manage_billing,
    ensure_organization_subscription,
    initialize_invoice_payment,
    issue_latest_usage_invoice,
    process_paystack_webhook,
    serialize_invoice,
    serialize_payment_attempt,
    serialize_plan,
    serialize_subscription,
    verify_payment_attempt,
)


def _organization_membership(
    user,
    organization_id,
    *,
    billing_authority: bool = False,
) -> OrganizationMembership:
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
    if billing_authority and not can_manage_billing(membership.role):
        raise PermissionDenied("Your organization role does not manage SchoolOS billing.")
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


class OrganizationInvoiceListView(APIView):
    """Commercial invoices are visible only to account billing authorities."""

    def get(self, request, organization_id):
        _organization_membership(
            request.user,
            organization_id,
            billing_authority=True,
        )
        invoices = (
            BillingInvoice.objects.filter(organization_id=organization_id)
            .prefetch_related("payment_attempts")
            .order_by("-issued_at", "-id")[:50]
        )
        return Response(
            {
                "invoices": [
                    serialize_invoice(invoice, include_attempts=True)
                    for invoice in invoices
                ]
            }
        )

    def post(self, request, organization_id):
        membership = _organization_membership(
            request.user,
            organization_id,
            billing_authority=True,
        )
        invoice = issue_latest_usage_invoice(membership.organization)
        return Response(
            serialize_invoice(invoice, include_attempts=True),
            status=status.HTTP_201_CREATED,
        )


class InvoiceCheckoutView(APIView):
    """Create a server-side Paystack checkout for one immutable invoice."""

    def post(self, request, organization_id, invoice_id):
        _organization_membership(
            request.user,
            organization_id,
            billing_authority=True,
        )
        try:
            invoice = BillingInvoice.objects.get(
                id=invoice_id,
                organization_id=organization_id,
            )
        except BillingInvoice.DoesNotExist as exc:
            raise PermissionDenied("This invoice is not available to your organization.") from exc
        attempt = initialize_invoice_payment(
            invoice,
            actor=request.user,
            email=request.user.email,
            provider_code="paystack",
        )
        return Response(
            {
                "invoice": serialize_invoice(invoice),
                "payment": serialize_payment_attempt(attempt),
            },
            status=status.HTTP_201_CREATED,
        )


class PaymentVerifyView(APIView):
    """Server-to-server transaction verification after returning from checkout."""

    def post(self, request, organization_id, reference):
        _organization_membership(
            request.user,
            organization_id,
            billing_authority=True,
        )
        try:
            attempt = PaymentAttempt.objects.select_related("invoice").get(
                reference=reference,
                invoice__organization_id=organization_id,
            )
        except PaymentAttempt.DoesNotExist as exc:
            raise PermissionDenied("This payment is not available to your organization.") from exc
        attempt = verify_payment_attempt(attempt)
        invoice = BillingInvoice.objects.get(pk=attempt.invoice_id)
        return Response(
            {
                "invoice": serialize_invoice(invoice, include_attempts=True),
                "payment": serialize_payment_attempt(attempt),
            }
        )


class PaystackWebhookView(APIView):
    """Signed, idempotent Paystack event receiver.

    This endpoint has no user authentication because Paystack calls it directly.
    Authenticity is established using Paystack's HMAC-SHA512 signature.
    """

    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = ()

    def post(self, request):
        result = process_paystack_webhook(
            request.body,
            request.headers.get("x-paystack-signature"),
        )
        return Response(result, status=status.HTTP_200_OK)
