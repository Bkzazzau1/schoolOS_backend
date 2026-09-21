from django.contrib import admin

from .models import AlumniProfile, AlumniVerificationEvent


@admin.register(AlumniProfile)
class AlumniProfileAdmin(admin.ModelAdmin):
    list_display = (
        "membership",
        "school",
        "graduation_year",
        "graduation_set",
        "verification_status",
        "directory_visible",
    )
    list_filter = ("school", "verification_status", "directory_visible", "graduation_year")
    search_fields = (
        "membership__user__email",
        "admission_number",
        "graduation_set",
        "profession",
        "organisation",
    )
    readonly_fields = (
        "verification_status",
        "submitted_at",
        "reviewed_at",
        "verification_note",
        "verified_at",
        "verified_by",
        "created_at",
        "updated_at",
    )


@admin.register(AlumniVerificationEvent)
class AlumniVerificationEventAdmin(admin.ModelAdmin):
    list_display = ("profile", "event", "actor", "at")
    list_filter = ("event", "profile__school")
    search_fields = (
        "profile__membership__user__email",
        "profile__admission_number",
        "note",
    )
    readonly_fields = ("profile", "event", "actor", "note", "at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
