from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """For support to see what a person was told. Messages are not edited here."""

    list_display = ("created_at", "school", "recipient", "kind", "title", "read_at")
    list_filter = ("school", "kind")
    search_fields = ("title", "message", "recipient__user__email")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
