from django.contrib import admin

from .models import Organization, OrganizationAuditEvent, OrganizationMembership


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    readonly_fields = ("created_at", "updated_at")


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "role", "is_active", "created_at")
    list_filter = ("role", "is_active", "organization")
    search_fields = ("user__email", "organization__name")
    autocomplete_fields = ("user", "organization")
    readonly_fields = ("created_at",)


@admin.register(OrganizationAuditEvent)
class OrganizationAuditEventAdmin(admin.ModelAdmin):
    list_display = ("organization", "action", "actor", "target_type", "target_id", "at")
    list_filter = ("action", "target_type", "organization")
    search_fields = ("organization__name", "actor__email", "target_id")
    readonly_fields = (
        "organization",
        "actor",
        "action",
        "target_type",
        "target_id",
        "detail",
        "at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
