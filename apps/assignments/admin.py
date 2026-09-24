from django.contrib import admin

from .models import (
    Assignment,
    AssignmentGradeEvent,
    AssignmentPublication,
    AssignmentRecipient,
    AssignmentSubmission,
    AssignmentSubmissionVersion,
)


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "external_id",
        "school",
        "class_subject",
        "term",
        "assignment_type",
        "state",
        "due_at",
    )
    list_filter = ("state", "assignment_type", "term")
    search_fields = (
        "external_id",
        "title",
        "class_subject__academic_class__name",
        "class_subject__subject__name",
    )
    readonly_fields = ("created_at", "updated_at", "published_at", "closed_at")


@admin.register(AssignmentRecipient)
class AssignmentRecipientAdmin(admin.ModelAdmin):
    list_display = ("assignment", "student_code", "student_name", "assigned_at")
    search_fields = ("student_code", "student_name", "assignment__title")


@admin.register(AssignmentSubmission)
class AssignmentSubmissionAdmin(admin.ModelAdmin):
    list_display = (
        "external_id",
        "assignment",
        "student",
        "state",
        "attempt_number",
        "is_late",
        "score",
        "submitted_at",
    )
    list_filter = ("state", "is_late")
    search_fields = (
        "external_id",
        "student__student_code",
        "student__first_name",
        "student__surname",
        "assignment__title",
    )


admin.site.register(AssignmentPublication)
admin.site.register(AssignmentSubmissionVersion)
admin.site.register(AssignmentGradeEvent)
