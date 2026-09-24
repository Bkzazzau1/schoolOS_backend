from django.contrib import admin

from .models import ReportCard, ReportCardEvent, ReportCardSubjectLine


class ReportCardSubjectLineInline(admin.TabularInline):
    model = ReportCardSubjectLine
    extra = 0
    readonly_fields = ("class_subject", "weighted_percent", "grade", "assessments_included")
    can_delete = False


@admin.register(ReportCard)
class ReportCardAdmin(admin.ModelAdmin):
    list_display = (
        "external_id",
        "school",
        "student",
        "term",
        "academic_class",
        "state",
        "overall_average",
        "overall_grade",
        "class_position",
    )
    list_filter = ("state", "term", "academic_class")
    search_fields = (
        "external_id",
        "student__student_code",
        "student__first_name",
        "student__surname",
    )
    readonly_fields = ("created_at", "updated_at", "generated_at", "submitted_at", "reviewed_at", "released_at")
    inlines = [ReportCardSubjectLineInline]


admin.site.register(ReportCardEvent)
