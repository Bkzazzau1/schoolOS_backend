"""The platform-operator console: SkulGen's own staff looking across every
organization, not a school's own proprietor/staff looking at their own school.

Gated by Django's own is_staff (the same account Django admin already uses -
no separate "platform staff" role exists or is needed), not the mobile app's
JWT/Membership system at all. An unauthenticated visit redirects to the same
login form /admin/ already uses.
"""

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.billing.models import OrganizationSubscription, Plan, SubscriptionStatus
from apps.billing.services import access_mode, admin_set_plan, admin_set_status, ensure_organization_subscription
from apps.organizations.models import Organization
from apps.schools.models import School

_SOURCE = "console"

#: Statuses worth a platform operator's attention first.
_ATTENTION_STATUSES = (
    SubscriptionStatus.PAST_DUE,
    SubscriptionStatus.GRACE,
    SubscriptionStatus.RESTRICTED,
    SubscriptionStatus.SUSPENDED,
)


@staff_member_required
def dashboard(request):
    subscriptions = OrganizationSubscription.objects.select_related("organization", "plan")
    status_counts = [
        {"status": status, "label": label, "count": subscriptions.filter(status=status).count()}
        for status, label in SubscriptionStatus.choices
    ]
    needs_attention = (
        subscriptions.filter(status__in=_ATTENTION_STATUSES)
        .order_by("status", "-updated_at")
    )
    return render(
        request,
        "console/dashboard.html",
        {
            "status_counts": status_counts,
            "needs_attention": needs_attention,
            "total_organizations": Organization.objects.filter(is_active=True).count(),
            "total_schools": School.objects.filter(is_active=True).count(),
        },
    )


@staff_member_required
def organization_list(request):
    q = request.GET.get("q", "").strip()
    status_filter = request.GET.get("status", "").strip()

    organizations = (
        Organization.objects.filter(is_active=True)
        .select_related("subscription", "subscription__plan")
        .annotate(school_count=Count("schools", distinct=True))
        .order_by("name")
    )
    if q:
        organizations = organizations.filter(name__icontains=q)
    if status_filter:
        organizations = organizations.filter(subscription__status=status_filter)

    return render(
        request,
        "console/organizations.html",
        {
            "organizations": organizations,
            "q": q,
            "status_filter": status_filter,
            "status_choices": SubscriptionStatus.choices,
        },
    )


@staff_member_required
def organization_detail(request, organization_id):
    organization = get_object_or_404(
        Organization.objects.select_related("subscription", "subscription__plan"), id=organization_id
    )
    subscription = ensure_organization_subscription(organization)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "set_status":
            new_status = request.POST.get("status", "")
            admin_set_status(subscription, new_status, actor_user=request.user, source=_SOURCE)
            messages.success(request, f"Subscription status set to {new_status}.")
        elif action == "set_plan":
            plan = get_object_or_404(Plan, id=request.POST.get("plan_id"))
            admin_set_plan(subscription, plan, actor_user=request.user, source=_SOURCE)
            messages.success(request, f"Plan changed to {plan.name}.")
        return redirect(reverse("console:organization_detail", args=[organization.id]))

    return render(
        request,
        "console/organization_detail.html",
        {
            "organization": organization,
            "subscription": subscription,
            "access_mode": access_mode(subscription),
            "schools": organization.schools.filter(is_active=True).order_by("name"),
            "invoices": subscription.invoices.all()[:20],
            "events": subscription.events.all()[:30],
            "plans": Plan.objects.filter(is_active=True).order_by("sort_order"),
            "status_choices": SubscriptionStatus.choices,
        },
    )
