"""The platform-operator console: SkulGen's own staff looking across every
organization, not a school's own proprietor/staff looking at their own school.

Gated by Django's own is_staff (the same account Django admin already uses -
no separate "platform staff" role exists or is needed), not the mobile app's
JWT/Membership system at all. An unauthenticated visit redirects to the same
login form /admin/ already uses.
"""

from datetime import datetime, time, timedelta

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.billing.census import correct_school_census
from apps.billing.cycle import automation_summary
from apps.billing.models import (
    CensusStatus,
    DuplicateCollectionReview,
    EnterpriseAgreement,
    OrganizationSubscription,
    PaymentAttempt,
    Plan,
    PricingAuditEvent,
    PricingBracket,
    SchoolEnrollmentCensus,
    SubscriptionStatus,
)
from apps.billing.pricing import (
    OverlappingBracketError,
    close_pricing_bracket,
    create_enterprise_agreement,
    create_pricing_bracket,
)
from apps.billing.reporting import (
    platform_subscription_report,
    revenue_by_plan,
    weekly_cancellations,
    weekly_confirmed_revenue,
)
from apps.billing.services import access_mode, admin_set_plan, admin_set_status, ensure_organization_subscription
from apps.organizations.models import Organization
from apps.schools.models import School

_SOURCE = "console"


def _minor_from_naira_input(raw: str) -> int:
    """An operator types a Naira amount (e.g. "40000" or "40000.50"); stored amounts are
    always minor units (kobo), so this is the one place that conversion happens."""

    try:
        naira = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Enter a plain Naira amount, e.g. 40000 or 40000.50.") from exc
    if naira < 0:
        raise ValueError("Amount cannot be negative.")
    return round(naira * 100)

#: Statuses worth a platform operator's attention first.
_ATTENTION_STATUSES = (
    SubscriptionStatus.PAST_DUE,
    SubscriptionStatus.GRACE,
    SubscriptionStatus.RESTRICTED,
    SubscriptionStatus.SUSPENDED,
)


def _weekly_school_growth(*, weeks: int = 12, now=None) -> list[dict]:
    """New schools created per calendar week (Mon-Sun), oldest first. Counts every
    school ever created in that week regardless of whether it's still active today -
    this answers "when did we grow", not "what's active now", so a later deactivation
    must never erase the week a school actually joined."""

    now = now or timezone.now()
    today = timezone.localdate(now)
    this_week_start = today - timedelta(days=today.weekday())
    buckets = []
    for weeks_ago in range(weeks - 1, -1, -1):
        start = this_week_start - timedelta(weeks=weeks_ago)
        end = start + timedelta(days=7)
        start_at = timezone.make_aware(datetime.combine(start, time.min))
        end_at = timezone.make_aware(datetime.combine(end, time.min))
        count = School.objects.filter(created_at__gte=start_at, created_at__lt=end_at).count()
        buckets.append({"week_start": start, "count": count})
    peak = max((bucket["count"] for bucket in buckets), default=0) or 1
    for bucket in buckets:
        bucket["height_pct"] = round(bucket["count"] / peak * 100)
    return buckets


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
    school_growth = _weekly_school_growth()
    cancellations = weekly_cancellations()
    return render(
        request,
        "console/dashboard.html",
        {
            "status_counts": status_counts,
            "needs_attention": needs_attention,
            "total_organizations": Organization.objects.filter(is_active=True).count(),
            "total_schools": School.objects.filter(is_active=True).count(),
            "report": platform_subscription_report(),
            "school_growth": school_growth,
            "school_growth_total": sum(bucket["count"] for bucket in school_growth),
            "revenue_growth": weekly_confirmed_revenue(),
            "revenue_by_plan": revenue_by_plan(),
            "cancellations": cancellations,
            "cancellations_total": sum(bucket["count"] for bucket in cancellations),
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

    invoices = list(subscription.invoices.prefetch_related("lines__school").all()[:20])
    payment_attempts = (
        PaymentAttempt.objects.filter(invoice__organization=organization)
        .select_related("invoice")
        .order_by("-created_at")[:20]
    )
    return render(
        request,
        "console/organization_detail.html",
        {
            "organization": organization,
            "subscription": subscription,
            "access_mode": access_mode(subscription),
            "automation": automation_summary(subscription),
            "schools": organization.schools.filter(is_active=True).order_by("name"),
            "invoices": invoices,
            "payment_attempts": payment_attempts,
            "events": subscription.events.all()[:30],
            "usage_snapshots": organization.usage_snapshots.all()[:20],
            "plans": Plan.objects.filter(is_active=True).order_by("sort_order"),
            "status_choices": SubscriptionStatus.choices,
        },
    )


@staff_member_required
def pricing_overview(request):
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create_bracket":
            try:
                create_pricing_bracket(
                    min_students=int(request.POST.get("min_students", "")),
                    max_students=int(request.POST["max_students"]) if request.POST.get("max_students") else None,
                    fixed_fee_minor=_minor_from_naira_input(request.POST.get("fixed_fee_naira", "")),
                    effective_from=timezone.now(),
                    actor=request.user,
                )
                messages.success(request, "Bracket created.")
            except (ValueError, OverlappingBracketError) as problem:
                messages.error(request, str(problem))
        elif action == "close_bracket":
            bracket = get_object_or_404(PricingBracket, id=request.POST.get("bracket_id"))
            try:
                close_pricing_bracket(bracket, actor=request.user)
                messages.success(request, "Bracket closed; a replacement for the same range can now be created.")
            except ValueError as problem:
                messages.error(request, str(problem))
        return redirect(reverse("console:pricing"))

    brackets = PricingBracket.objects.order_by("min_students", "-effective_from")
    agreements = (
        EnterpriseAgreement.objects.select_related("school", "school__organization")
        .order_by("-effective_from")[:30]
    )
    audit_events = PricingAuditEvent.objects.all()[:30]
    return render(
        request,
        "console/pricing.html",
        {
            "brackets": brackets,
            "agreements": agreements,
            "audit_events": audit_events,
        },
    )


@staff_member_required
def school_enterprise_agreement(request, school_id):
    school = get_object_or_404(School.objects.select_related("organization"), id=school_id)

    if request.method == "POST":
        try:
            create_enterprise_agreement(
                school=school,
                fixed_fee_minor=_minor_from_naira_input(request.POST.get("fixed_fee_naira", "")),
                effective_from=timezone.now(),
                contract_reference=request.POST.get("contract_reference", "").strip(),
                notes=request.POST.get("notes", "").strip(),
                actor=request.user,
            )
            messages.success(request, "Enterprise agreement created.")
        except ValueError as problem:
            messages.error(request, str(problem))
        return redirect(reverse("console:school_enterprise_agreement", args=[school.id]))

    agreements = school.enterprise_agreements.order_by("-effective_from")
    return render(
        request,
        "console/school_enterprise_agreement.html",
        {
            "school": school,
            "agreements": agreements,
        },
    )


@staff_member_required
def census_exceptions(request):
    """Every school currently flagged needs_review (Phase D Stage 3, Task 7 item 4) - zero
    enrollment, or above every bracket with no enterprise agreement - with a documented,
    auditable correction path (apps.billing.census.correct_school_census) that never
    silently rewrites a paid invoice."""

    if request.method == "POST":
        census = get_object_or_404(SchoolEnrollmentCensus, id=request.POST.get("census_id"))
        try:
            count = int(request.POST.get("billable_student_count", ""))
            correct_school_census(
                census, actor=request.user,
                reason=request.POST.get("reason", "").strip(),
                billable_student_count=count,
            )
            messages.success(request, "Census corrected.")
        except ValueError as problem:
            messages.error(request, str(problem) if str(problem) else "Enter a whole number for the corrected count.")
        return redirect(reverse("console:census_exceptions"))

    exceptions = (
        SchoolEnrollmentCensus.objects.filter(status=CensusStatus.NEEDS_REVIEW)
        .select_related("school", "school__organization")
        .order_by("-created_at")[:50]
    )
    return render(request, "console/census_exceptions.html", {"exceptions": exceptions})


@staff_member_required
def duplicate_payments(request):
    """Every invoice with more than one genuinely successful provider collection (Phase D
    Stage 2's DuplicateCollectionReview, surfaced here for Stage 3 Task 7 item 7) - never
    auto-refunded, only recorded for an operator to resolve by contacting the payer or
    processing a manual refund under an approved policy."""

    if request.method == "POST":
        review = get_object_or_404(DuplicateCollectionReview, id=request.POST.get("review_id"))
        note = request.POST.get("resolution_note", "").strip()
        if not note:
            messages.error(request, "A resolution note is required.")
        else:
            review.resolution_note = note
            review.resolved_at = timezone.now()
            review.resolved_by = request.user
            review.save(update_fields=["resolution_note", "resolved_at", "resolved_by"])
            messages.success(request, "Marked resolved.")
        return redirect(reverse("console:duplicate_payments"))

    reviews = (
        DuplicateCollectionReview.objects.select_related(
            "invoice", "invoice__organization", "primary_attempt", "duplicate_attempt",
        )
        .order_by("-detected_at")[:50]
    )
    return render(request, "console/duplicate_payments.html", {"reviews": reviews})
