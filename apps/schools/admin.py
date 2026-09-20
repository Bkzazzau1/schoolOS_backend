from django.contrib import admin

from .models import Membership, School


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "created_at")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name", "slug")


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "school", "role", "is_active")
    list_filter = ("school", "role", "is_active")
    search_fields = ("user__email", "school__name")
    autocomplete_fields = ("user",)
