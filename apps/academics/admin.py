from django.contrib import admin

from .models import (
    AcademicClass,
    AcademicSession,
    AcademicTerm,
    ClassSubject,
    CurriculumTopic,
    EnrollmentAcademicContext,
    ProgressionBatch,
    ProgressionDecision,
    StudentSubjectSelection,
    Subject,
    TeachingAssignment,
)


@admin.register(AcademicSession)
class AcademicSessionAdmin(admin.ModelAdmin):
    list_display = ("school", "name", "code", "status", "starts_on", "ends_on")
    list_filter = ("status", "school")
    search_fields = ("name", "code", "school__name")


@admin.register(AcademicTerm)
class AcademicTermAdmin(admin.ModelAdmin):
    list_display = ("session", "name", "sequence", "status", "starts_on", "ends_on")
    list_filter = ("status", "session__school", "session")
    search_fields = ("name", "code", "session__name", "session__school__name")
    list_select_related = ("session", "session__school")


@admin.register(AcademicClass)
class AcademicClassAdmin(admin.ModelAdmin):
    list_display = (
        "school",
        "name",
        "section",
        "level_order",
        "next_class",
        "is_terminal",
        "is_active",
    )
    list_filter = ("school", "section", "is_terminal", "is_active")
    search_fields = ("name", "code", "school__name")


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ("school", "code", "name", "section", "is_active")
    list_filter = ("school", "section", "is_active")
    search_fields = ("code", "name", "short_name", "school__name")
    list_select_related = ("school",)


@admin.register(ClassSubject)
class ClassSubjectAdmin(admin.ModelAdmin):
    list_display = (
        "session",
        "academic_class",
        "subject",
        "requirement",
        "periods_per_week",
        "is_active",
    )
    list_filter = (
        "session__school",
        "session",
        "academic_class",
        "subject",
        "requirement",
        "is_active",
    )
    search_fields = (
        "session__name",
        "academic_class__name",
        "academic_class__code",
        "subject__name",
        "subject__code",
    )
    list_select_related = ("session", "academic_class", "subject")


@admin.register(CurriculumTopic)
class CurriculumTopicAdmin(admin.ModelAdmin):
    list_display = ("class_subject", "term", "sequence", "title")
    list_filter = (
        "class_subject__session__school",
        "class_subject__session",
        "class_subject__academic_class",
        "class_subject__subject",
        "term",
    )
    search_fields = (
        "title",
        "description",
        "class_subject__subject__name",
        "class_subject__academic_class__name",
    )
    list_select_related = (
        "term",
        "class_subject__session",
        "class_subject__academic_class",
        "class_subject__subject",
    )


class TeachingAssignmentStateFilter(admin.SimpleListFilter):
    title = "assignment state"
    parameter_name = "assignment_state"

    def lookups(self, request, model_admin):
        return (("current", "Current"), ("history", "History"))

    def queryset(self, request, queryset):
        if self.value() == "current":
            return queryset.filter(ended_at__isnull=True)
        if self.value() == "history":
            return queryset.filter(ended_at__isnull=False)
        return queryset


@admin.register(TeachingAssignment)
class TeachingAssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "school",
        "class_subject",
        "teacher_membership",
        "started_at",
        "ended_at",
        "previous_assignment",
    )
    list_filter = (
        "school",
        "class_subject__session",
        "class_subject__academic_class",
        "class_subject__subject",
        "teacher_membership",
        TeachingAssignmentStateFilter,
    )
    search_fields = (
        "external_id",
        "class_subject__academic_class__name",
        "class_subject__subject__name",
        "teacher_membership__user__email",
    )
    readonly_fields = ("previous_assignment", "created_at", "updated_at")
    list_select_related = (
        "school",
        "teacher_membership__user",
        "class_subject__session",
        "class_subject__academic_class",
        "class_subject__subject",
        "previous_assignment",
    )


@admin.register(StudentSubjectSelection)
class StudentSubjectSelectionAdmin(admin.ModelAdmin):
    list_display = (
        "enrollment_context",
        "class_subject",
        "selected_by",
        "selected_at",
    )
    list_filter = (
        "class_subject__session__school",
        "class_subject__session",
        "class_subject__academic_class",
        "class_subject__subject",
    )
    search_fields = (
        "enrollment_context__enrollment__student__student_code",
        "enrollment_context__enrollment__student__first_name",
        "enrollment_context__enrollment__student__surname",
        "class_subject__subject__name",
    )
    list_select_related = (
        "enrollment_context__enrollment__student",
        "class_subject__session",
        "class_subject__academic_class",
        "class_subject__subject",
        "selected_by",
    )


@admin.register(EnrollmentAcademicContext)
class EnrollmentAcademicContextAdmin(admin.ModelAdmin):
    list_display = ("enrollment", "session", "academic_class", "entry_term", "source")
    list_filter = ("session__school", "session", "academic_class")
    search_fields = (
        "enrollment__student__student_code",
        "enrollment__student__first_name",
        "enrollment__student__surname",
    )
    list_select_related = (
        "enrollment__student",
        "session",
        "academic_class",
        "entry_term",
    )


class ProgressionDecisionInline(admin.TabularInline):
    model = ProgressionDecision
    extra = 0
    readonly_fields = (
        "student",
        "outcome",
        "target_class",
        "records_pack_ready",
        "note",
    )


@admin.register(ProgressionBatch)
class ProgressionBatchAdmin(admin.ModelAdmin):
    list_display = (
        "school",
        "external_id",
        "from_session",
        "to_session",
        "source_class",
        "status",
        "applied_at",
    )
    list_filter = ("status", "school")
    search_fields = ("external_id", "school__name", "source_class__name")
    inlines = (ProgressionDecisionInline,)
