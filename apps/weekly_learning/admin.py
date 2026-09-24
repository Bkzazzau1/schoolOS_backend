from django.contrib import admin

from .models import WeeklyLearningPublication, WeeklyLearningUpdate


class WeeklyLearningPublicationInline(admin.TabularInline):
    model = WeeklyLearningPublication
    extra = 0
    can_delete = False
    readonly_fields = (
        "revision",
        "published_by",
        "published_at",
        "snapshot",
    )


@admin.register(WeeklyLearningUpdate)
class WeeklyLearningUpdateAdmin(admin.ModelAdmin):
    list_display = (
        "external_id",
        "class_subject",
        "term",
        "week_start",
        "state",
        "author_membership",
        "published_at",
    )
    list_filter = ("state", "term", "week_start")
    search_fields = (
        "external_id",
        "class_subject__academic_class__name",
        "class_subject__subject__name",
        "author_membership__user__email",
    )
    readonly_fields = (
        "version",
        "published_at",
        "created_at",
        "updated_at",
    )
    inlines = [WeeklyLearningPublicationInline]


@admin.register(WeeklyLearningPublication)
class WeeklyLearningPublicationAdmin(admin.ModelAdmin):
    list_display = ("update", "revision", "published_by", "published_at")
    readonly_fields = ("update", "revision", "snapshot", "published_by", "published_at")
