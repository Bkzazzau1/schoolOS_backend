from django.contrib import admin

from .models import ClassTeacherAssignment


@admin.register(ClassTeacherAssignment)
class ClassTeacherAssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "external_id",
        "school",
        "academic_class",
        "session",
        "teacher_membership",
        "started_at",
        "ended_at",
    )
    list_filter = ("session", "academic_class")
    search_fields = ("external_id", "academic_class__name", "teacher_membership__user__email")
    readonly_fields = ("created_at", "updated_at")
