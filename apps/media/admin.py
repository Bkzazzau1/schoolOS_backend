from django.contrib import admin

from .models import MediaAsset, MediaAuditEvent, MediaDerivative, MediaJob


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MediaAsset)
class MediaAssetAdmin(ReadOnlyAdmin):
    list_display = ("id", "school", "owner_type", "owner_id", "category", "media_type", "status", "byte_size", "created_at")
    list_filter = ("status", "media_type", "storage_provider")
    search_fields = ("id", "owner_id", "original_filename")


@admin.register(MediaDerivative)
class MediaDerivativeAdmin(ReadOnlyAdmin):
    list_display = ("id", "asset", "kind", "width", "height", "byte_size")


@admin.register(MediaJob)
class MediaJobAdmin(ReadOnlyAdmin):
    list_display = ("id", "asset", "kind", "status", "attempts", "run_after")
    list_filter = ("kind", "status")


@admin.register(MediaAuditEvent)
class MediaAuditEventAdmin(ReadOnlyAdmin):
    list_display = ("id", "school", "kind", "object_type", "object_id", "at")
    list_filter = ("kind",)
