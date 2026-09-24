from django.contrib import admin

from .models import (
    AssessmentDefinition,
    AssessmentEvent,
    AssessmentRecipient,
    AssessmentScore,
)


@admin.register(AssessmentDefinition)
class AssessmentDefinitionAdmin(admin.ModelAdmin):
    list_display = (
        "external_id",
        "school",
        "class_subject",
        "term",
        "assessment_type",
        "state",
        "maximum_score",
        "weight",
    )
    list_filter = ("state", "assessment_type", "term")
    search_fields = (
        "external_id",
        "title",
        "class_subject__academic_class__name",
        "class_subject__subject__name",
    )
    readonly_fields = ("created_at", "updated_at", "submitted_at", "locked_at", "released_at")


@admin.register(AssessmentRecipient)
class AssessmentRecipientAdmin(admin.ModelAdmin):
    list_display = ("assessment", "student_code", "student_name", "assigned_at")
    search_fields = ("student_code", "student_name", "assessment__title")


@admin.register(AssessmentScore)
class AssessmentScoreAdmin(admin.ModelAdmin):
    list_display = ("assessment", "student", "score", "entered_at")
    search_fields = ("student__student_code", "student__first_name", "student__surname")


@admin.register(AssessmentEvent)
class AssessmentEventAdmin(admin.ModelAdmin):
    list_display = ("assessment", "revision", "action", "actor_membership", "created_at")
    list_filter = ("action",)
    search_fields = ("assessment__external_id", "assessment__title")
