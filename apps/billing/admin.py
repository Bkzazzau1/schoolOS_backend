from django.contrib import admin

from .models import (
    OrganizationSubscription,
    Plan,
    PlanEntitlement,
    SubscriptionEvent,
    UsageSnapshot,
)


class PlanEntitlementInline(admin.TabularInline):
    model = PlanEntitlement
    extra = 0


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "currency",
        "billing_interval",
        "student_unit_amount_minor",
        "is_active",
        "is_public",
    )
    list_filter = ("is_active", "is_public", "billing_interval", "currency")
    search_fields = ("code", "name")
    inlines = (PlanEntitlementInline,)


@admin.register(OrganizationSubscription)
class OrganizationSubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "plan",
        "status",
        "current_period_end",
        "grace_ends_at",
        "provider",
        "updated_at",
    )
    list_filter = ("status", "provider", "plan")
    search_fields = (
        "organization__name",
        "provider_customer_ref",
        "provider_subscription_ref",
    )
    readonly_fields = ("created_at", "updated_at")

    def save_model(self, request, obj, form, change):
        previous = None
        if change and obj.pk:
            previous = OrganizationSubscription.objects.filter(pk=obj.pk).values(
                "status", "plan_id"
            ).first()
        super().save_model(request, obj, form, change)

        actor = {
            "adminUserId": str(request.user.id),
            "adminEmail": request.user.email,
        }
        if previous is None:
            SubscriptionEvent.objects.create(
                subscription=obj,
                event="admin_subscription_created",
                to_status=obj.status,
                detail={**actor, "planId": str(obj.plan_id) if obj.plan_id else None},
            )
            return

        if previous["status"] != obj.status:
            SubscriptionEvent.objects.create(
                subscription=obj,
                event="admin_status_changed",
                from_status=previous["status"],
                to_status=obj.status,
                detail=actor,
            )
        if previous["plan_id"] != obj.plan_id:
            SubscriptionEvent.objects.create(
                subscription=obj,
                event="admin_plan_changed",
                from_status=obj.status,
                to_status=obj.status,
                detail={
                    **actor,
                    "fromPlanId": (
                        str(previous["plan_id"]) if previous["plan_id"] else None
                    ),
                    "toPlanId": str(obj.plan_id) if obj.plan_id else None,
                },
            )


@admin.register(SubscriptionEvent)
class SubscriptionEventAdmin(admin.ModelAdmin):
    list_display = ("subscription", "event", "from_status", "to_status", "at")
    list_filter = ("event", "from_status", "to_status")
    search_fields = ("subscription__organization__name", "event")
    readonly_fields = (
        "subscription",
        "event",
        "from_status",
        "to_status",
        "detail",
        "at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(UsageSnapshot)
class UsageSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "active_school_count",
        "billable_student_count",
        "source",
        "captured_at",
    )
    list_filter = ("source",)
    search_fields = ("organization__name",)
    readonly_fields = (
        "organization",
        "subscription",
        "period_start",
        "period_end",
        "active_school_count",
        "billable_student_count",
        "source",
        "metadata",
        "captured_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
