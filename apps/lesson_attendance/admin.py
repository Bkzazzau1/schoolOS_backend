from django.contrib import admin

from .models import LessonAttendanceEntry, LessonAttendanceRegister


class LessonAttendanceEntryInline(admin.TabularInline):
    model = LessonAttendanceEntry
    extra = 0
    can_delete = False
    readonly_fields = (
        "student",
        "student_code",
        "student_name",
        "status",
        "note",
        "created_at",
        "updated_at",
    )


@admin.register(LessonAttendanceRegister)
class LessonAttendanceRegisterAdmin(admin.ModelAdmin):
    list_display = (
        "lesson_date",
        "school",
        "timetable_entry",
        "teacher_membership",
        "state",
        "submitted_at",
    )
    list_filter = (
        "state",
        "lesson_date",
        "school",
        "timetable_entry__class_subject__academic_class",
        "timetable_entry__class_subject__subject",
    )
    search_fields = (
        "external_id",
        "timetable_entry__external_id",
        "teacher_membership__user__email",
        "entries__student_code",
        "entries__student_name",
    )
    raw_id_fields = (
        "timetable_entry",
        "teacher_membership",
        "curriculum_topic",
        "submitted_by",
    )
    readonly_fields = ("created_at", "updated_at", "submitted_at")
    inlines = [LessonAttendanceEntryInline]


@admin.register(LessonAttendanceEntry)
class LessonAttendanceEntryAdmin(admin.ModelAdmin):
    list_display = (
        "register",
        "student_code",
        "student_name",
        "status",
    )
    list_filter = (
        "status",
        "register__lesson_date",
        "register__school",
    )
    search_fields = ("student_code", "student_name", "register__external_id")
    raw_id_fields = ("register", "student")
    readonly_fields = ("created_at", "updated_at")
