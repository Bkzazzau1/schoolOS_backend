from django.contrib import admin

from .models import InvitationEvent, StaffInvitation, StaffLink


class ReadOnly(admin.ModelAdmin):
    """Invitations are made and changed only by the feature, which keeps the audit
    trail. Support can look, not edit."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class EventInline(admin.TabularInline):
    model = InvitationEvent
    extra = 0
    can_delete = False
    readonly_fields = ("at", "event", "ip", "user_agent", "detail")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(StaffInvitation)
class StaffInvitationAdmin(ReadOnly):
    list_display = ("staff_id", "email", "school", "role", "status", "sent_at", "expires_at")
    list_filter = ("school", "status", "role")
    search_fields = ("staff_id", "email")
    exclude = ("token_hash",)  # a hash, but there is no reason to show it
    inlines = [EventInline]


@admin.register(StaffLink)
class StaffLinkAdmin(ReadOnly):
    list_display = ("staff_id", "membership", "school", "linked_at")
    list_filter = ("school",)
    search_fields = ("staff_id",)
