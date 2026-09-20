from django.contrib import admin

from .models import IdentityClaim


@admin.register(IdentityClaim)
class IdentityClaimAdmin(admin.ModelAdmin):
    """Who holds each phone number and NIN. For support to look up a duplicate; the
    table is maintained by the staff feature and is read-only here."""

    list_display = ("school", "kind", "value", "holder_type", "holder_id", "holder_name")
    list_filter = ("school", "kind", "holder_type")
    search_fields = ("value", "holder_name", "holder_id")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
