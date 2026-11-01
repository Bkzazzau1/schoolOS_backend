from django.urls import path

from .views import (
    InvoiceCheckoutView,
    OrganizationInvoiceListView,
    OrganizationSubscriptionView,
    PaymentVerifyView,
    PaystackWebhookView,
    PlanListView,
    SchoolEntitlementView,
)

urlpatterns = [
    path("plans/", PlanListView.as_view()),
    path(
        "organizations/<uuid:organization_id>/subscription/",
        OrganizationSubscriptionView.as_view(),
    ),
    path(
        "schools/<uuid:school_id>/billing/entitlements/",
        SchoolEntitlementView.as_view(),
    ),
    path(
        "organizations/<uuid:organization_id>/billing/invoices/",
        OrganizationInvoiceListView.as_view(),
    ),
    path(
        "organizations/<uuid:organization_id>/billing/invoices/<uuid:invoice_id>/checkout/",
        InvoiceCheckoutView.as_view(),
    ),
    path(
        "organizations/<uuid:organization_id>/billing/payments/<str:reference>/verify/",
        PaymentVerifyView.as_view(),
    ),
    path("billing/webhooks/paystack/", PaystackWebhookView.as_view()),
]
