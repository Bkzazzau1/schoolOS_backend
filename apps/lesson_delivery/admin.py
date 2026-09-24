from django.contrib import admin

from .models import LessonDeliveryRecord, LessonPlan, LessonPlanReview


@admin.register(LessonPlan)
class LessonPlanAdmin(admin.ModelAdmin):
    list_display = (
        "school",
        "lesson_date",
        "class_subject",
        "curriculum_topic",
        "author_membership",
        "state",
        "version",
    )
    list_filter = (
        "school",
        "state",
        "class_subject__session",
        "class_subject__academic_class",
        "class_subject__subject",
    )
    search_fields = (
        "external_id",
        "class_subject__academic_class__name",
        "class_subject__subject__name",
        "curriculum_topic__title",
        "author_membership__user__email",
    )
    readonly_fields = (
        "author_membership",
        "last_edited_by",
        "submitted_by",
        "submitted_at",
        "reviewed_at",
        "reviewed_by",
        "review_comment",
        "created_at",
        "updated_at",
    )
    list_select_related = (
        "school",
        "timetable_entry",
        "class_subject__academic_class",
        "class_subject__subject",
        "curriculum_topic",
        "author_membership__user",
        "reviewed_by__user",
    )


@admin.register(LessonPlanReview)
class LessonPlanReviewAdmin(admin.ModelAdmin):
    list_display = (
        "school",
        "plan",
        "plan_version",
        "decision",
        "reviewer_membership",
        "reviewed_at",
    )
    list_filter = ("school", "decision", "reviewer_membership")
    search_fields = (
        "external_id",
        "plan__external_id",
        "reviewer_membership__user__email",
        "comment",
    )
    readonly_fields = (
        "school",
        "external_id",
        "plan",
        "reviewer_membership",
        "plan_version",
        "decision",
        "comment",
        "reviewed_at",
    )


@admin.register(LessonDeliveryRecord)
class LessonDeliveryRecordAdmin(admin.ModelAdmin):
    list_display = (
        "school",
        "lesson_date",
        "plan",
        "curriculum_topic",
        "teacher_membership",
        "state",
        "topic_completed",
        "delivered_at",
    )
    list_filter = (
        "school",
        "state",
        "topic_completed",
        "curriculum_topic__term",
        "curriculum_topic__class_subject__academic_class",
    )
    search_fields = (
        "external_id",
        "plan__external_id",
        "curriculum_topic__title",
        "teacher_membership__user__email",
    )
    readonly_fields = (
        "teacher_membership",
        "attendance_register",
        "delivered_at",
        "created_at",
        "updated_at",
    )
    list_select_related = (
        "school",
        "timetable_entry",
        "plan",
        "curriculum_topic",
        "teacher_membership__user",
        "attendance_register",
    )
