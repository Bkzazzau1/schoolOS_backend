from django.contrib import admin

from .models import TimetableEntry, TimetableOverride


@admin.register(TimetableEntry)
class TimetableEntryAdmin(admin.ModelAdmin):
    list_display = (
        "school",
        "term",
        "day_of_week",
        "period_number",
        "class_subject",
        "starts_at",
        "ends_at",
        "room",
        "is_active",
    )
    list_filter = (
        "school",
        "term",
        "day_of_week",
        "class_subject__academic_class",
        "class_subject__subject",
        "is_active",
    )
    search_fields = (
        "external_id",
        "class_subject__academic_class__name",
        "class_subject__subject__name",
        "room",
    )
    list_select_related = (
        "school",
        "term__session",
        "class_subject__academic_class",
        "class_subject__subject",
    )


@admin.register(TimetableOverride)
class TimetableOverrideAdmin(admin.ModelAdmin):
    list_display = (
        "school",
        "lesson_date",
        "timetable_entry",
        "substitute_teacher_membership",
        "room",
        "is_cancelled",
    )
    list_filter = (
        "school",
        "lesson_date",
        "is_cancelled",
        "timetable_entry__term",
        "timetable_entry__class_subject__academic_class",
    )
    search_fields = (
        "external_id",
        "timetable_entry__class_subject__academic_class__name",
        "timetable_entry__class_subject__subject__name",
        "substitute_teacher_membership__user__email",
        "room",
        "note",
    )
    list_select_related = (
        "school",
        "timetable_entry__term",
        "timetable_entry__class_subject__academic_class",
        "timetable_entry__class_subject__subject",
        "substitute_teacher_membership__user",
    )
