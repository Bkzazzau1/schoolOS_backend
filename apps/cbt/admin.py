from django.contrib import admin

from .models import CbtAttempt, CbtEvent, CbtQuestion, CbtRecipient, CbtTest


class CbtQuestionInline(admin.TabularInline):
    model = CbtQuestion
    extra = 0
    readonly_fields = ("sequence", "prompt", "options", "correct_index", "explanation")
    can_delete = False


@admin.register(CbtTest)
class CbtTestAdmin(admin.ModelAdmin):
    list_display = (
        "external_id",
        "school",
        "class_subject",
        "term",
        "state",
        "duration_minutes",
    )
    list_filter = ("state", "term")
    search_fields = (
        "external_id",
        "title",
        "class_subject__academic_class__name",
        "class_subject__subject__name",
    )
    readonly_fields = ("created_at", "updated_at", "published_at", "closed_at")
    inlines = [CbtQuestionInline]


@admin.register(CbtRecipient)
class CbtRecipientAdmin(admin.ModelAdmin):
    list_display = ("test", "student_code", "student_name", "assigned_at")
    search_fields = ("student_code", "student_name", "test__title")


@admin.register(CbtAttempt)
class CbtAttemptAdmin(admin.ModelAdmin):
    list_display = ("test", "student", "started_at", "submitted_at", "score")
    list_filter = ("test",)
    search_fields = ("student__student_code", "student__first_name", "student__surname", "test__title")


admin.site.register(CbtEvent)
