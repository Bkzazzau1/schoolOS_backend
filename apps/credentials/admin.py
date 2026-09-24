from django.contrib import admin

from .models import CredentialAuditEvent, CredentialRecoveryRequest


@admin.register(CredentialRecoveryRequest)
class CredentialRecoveryRequestAdmin(admin.ModelAdmin):
    list_display = ("school", "user", "identity_kind", "status", "created_at")
    list_filter = ("status", "identity_kind", "school")
    search_fields = ("requested_identifier", "user__email", "user__first_name", "user__last_name")
    readonly_fields = (
        "id",
        "school",
        "user",
        "identity_kind",
        "requested_identifier",
        "status",
        "note",
        "created_at",
        "resolved_at",
        "resolved_by",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CredentialAuditEvent)
class CredentialAuditEventAdmin(admin.ModelAdmin):
    list_display = ("event", "school", "user", "actor", "created_at")
    list_filter = ("event", "school")
    search_fields = ("user__email", "user__first_name", "user__last_name")
    readonly_fields = ("id", "school", "user", "actor", "event", "detail", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
