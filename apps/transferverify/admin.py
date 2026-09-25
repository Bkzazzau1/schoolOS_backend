from django.contrib import admin

from .models import (
    AssociationAdministrator,
    BadDebtClassification,
    BadDebtEvent,
    SchoolAssociationMembership,
    SchoolProprietorAssociation,
)


@admin.register(BadDebtClassification)
class BadDebtClassificationAdmin(admin.ModelAdmin):
    list_display = ("external_id", "school", "student", "status", "outstanding_amount_minor", "classified_at")
    list_filter = ("status", "school")
    search_fields = ("external_id", "student__first_name", "student__surname", "student__student_code")
    readonly_fields = ("created_at", "updated_at", "classified_at", "resolved_at")


admin.site.register(BadDebtEvent)


@admin.register(SchoolProprietorAssociation)
class SchoolProprietorAssociationAdmin(admin.ModelAdmin):
    """Associations are created and verified by SchoolOS staff here - a
    school only ever joins one afterwards, through the live join-request API
    an association administrator then approves."""

    list_display = ("name", "status", "geographic_scope", "created_at")
    list_filter = ("status",)
    search_fields = ("name", "registration_reference")
    readonly_fields = ("created_at", "updated_at")


@admin.register(SchoolAssociationMembership)
class SchoolAssociationMembershipAdmin(admin.ModelAdmin):
    list_display = ("school", "association", "status", "requested_at", "decided_at")
    list_filter = ("status", "association")
    search_fields = ("school__name", "association__name")
    readonly_fields = ("requested_at", "updated_at")


@admin.register(AssociationAdministrator)
class AssociationAdministratorAdmin(admin.ModelAdmin):
    list_display = ("user", "association", "is_active", "added_at")
    list_filter = ("association", "is_active")
    search_fields = ("user__email",)
    readonly_fields = ("added_at",)
