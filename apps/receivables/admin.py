from django.contrib import admin

from .models import Family, FamilyGuardian, FamilyStudent, FinanceAuditEvent


class ReadOnlyAdmin(admin.ModelAdmin):
    """Financial records are inspected here, never changed: every change goes through the services
    that enforce the invariants and write the audit trail, so the admin is not a way around them."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Family)
class FamilyAdmin(ReadOnlyAdmin):
    list_display = ("code", "display_name", "school", "status", "merged_into", "created_at")
    list_filter = ("status", "school")
    search_fields = ("code", "display_name")


@admin.register(FamilyStudent)
class FamilyStudentAdmin(ReadOnlyAdmin):
    list_display = ("family", "student", "is_active", "joined_at", "left_at")
    list_filter = ("is_active", "school")


@admin.register(FamilyGuardian)
class FamilyGuardianAdmin(ReadOnlyAdmin):
    list_display = ("family", "guardian", "is_primary_payer", "is_active")
    list_filter = ("is_active", "school")


@admin.register(FinanceAuditEvent)
class FinanceAuditEventAdmin(ReadOnlyAdmin):
    list_display = ("at", "school", "kind", "object_type", "object_id", "actor")
    list_filter = ("kind", "school")
    search_fields = ("object_id",)
