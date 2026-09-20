from django.contrib import admin

from .models import AccessChange, MembershipActivity, RoleActivity


class ReadOnlyAdmin(admin.ModelAdmin):
    """Access is changed only through the owner's endpoints, which keep the audit
    trail. The admin can look, not edit."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AccessChange)
class AccessChangeAdmin(ReadOnlyAdmin):
    list_display = ("at", "school", "kind", "activity", "role", "actor", "target")
    list_filter = ("school", "kind")
    search_fields = ("activity",)


@admin.register(MembershipActivity)
class MembershipActivityAdmin(ReadOnlyAdmin):
    list_display = ("membership", "activity", "effect", "expires_at", "set_by", "set_at")
    list_filter = ("effect",)


@admin.register(RoleActivity)
class RoleActivityAdmin(ReadOnlyAdmin):
    list_display = ("school", "role", "activity", "enabled")
    list_filter = ("school", "role", "enabled")
