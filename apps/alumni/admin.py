from django.contrib import admin

from .models import AlumniProfile


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
