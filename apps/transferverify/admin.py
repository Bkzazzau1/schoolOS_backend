from django.contrib import admin

from .models import BadDebtClassification, BadDebtEvent


@admin.register(BadDebtClassification)
class BadDebtClassificationAdmin(admin.ModelAdmin):
    list_display = ("external_id", "school", "student", "status", "outstanding_amount_minor", "classified_at")
    list_filter = ("status", "school")
    search_fields = ("external_id", "student__first_name", "student__surname", "student__student_code")
    readonly_fields = ("created_at", "updated_at", "classified_at", "resolved_at")


admin.site.register(BadDebtEvent)
