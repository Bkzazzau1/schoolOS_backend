from django.contrib import admin

from .models import MutationLog, SyncRecord


@admin.register(SyncRecord)
class SyncRecordAdmin(admin.ModelAdmin):
    list_display = ("entity_type", "entity_id", "school", "version", "deleted", "updated_at")
    list_filter = ("school", "entity_type", "deleted")
    search_fields = ("entity_id",)
    # Records are written only through the sync API. The admin is read-only so
    # the audit trail and version numbers cannot be quietly changed.
    readonly_fields = [f.name for f in SyncRecord._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MutationLog)
class MutationLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "school", "membership", "entity_type", "entity_id", "operation", "disposition")
    list_filter = ("school", "disposition", "entity_type")
    search_fields = ("entity_id", "mutation_id")
    readonly_fields = [f.name for f in MutationLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
