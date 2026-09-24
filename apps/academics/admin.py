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
    list_filter = ("status",)
    search_fields = ("name", "code", "session__name", "session__school__name")


@admin.register(AcademicClass)
class AcademicClassAdmin(admin.ModelAdmin):
    list_display = ("school", "name", "section", "level_order", "next_class", "is_terminal", "is_active")
    list_filter = ("section", "is_terminal", "is_active", "school")
    search_fields = ("name", "code", "school__name")


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ("school", "code", "name", "section", "is_active")
    list_filter = ("school", "section", "is_active")
    search_fields = ("code", "name", "school__name")


@admin.register(ClassSubject)
class ClassSubjectAdmin(admin.ModelAdmin):
    list_display = ("session", "academic_class", "subject", "requirement", "periods_per_week", "is_active")
    list_filter = ("session", "requirement", "is_active")
    search_fields = ("academic_class__name", "subject__name", "subject__code")


@admin.register(CurriculumTopic)
class CurriculumTopicAdmin(admin.ModelAdmin):
    list_display = ("class_subject", "term", "sequence", "title")
    list_filter = ("term",)
    search_fields = ("title", "class_subject__subject__name", "class_subject__academic_class__name")


@admin.register(TeachingAssignment)
class TeachingAssignmentAdmin(admin.ModelAdmin):
    list_display = ("school", "class_subject", "teacher_membership", "started_at", "ended_at")
    list_filter = ("school", "class_subject__session")
    search_fields = ("external_id", "class_subject__academic_class__name", "class_subject__subject__name", "teacher_membership__user__email")
    readonly_fields = ("previous_assignment", "created_at", "updated_at")


@admin.register(StudentSubjectSelection)
class StudentSubjectSelectionAdmin(admin.ModelAdmin):
    list_display = ("enrollment_context", "class_subject", "selected_at")
    list_filter = ("class_subject__session", "class_subject__academic_class")
    search_fields = ("enrollment_context__enrollment__student__student_code", "class_subject__subject__name")


@admin.register(EnrollmentAcademicContext)
class EnrollmentAcademicContextAdmin(admin.ModelAdmin):
    list_display = ("enrollment", "session", "academic_class", "entry_term", "source")
    list_filter = ("session", "academic_class")


class ProgressionDecisionInline(admin.TabularInline):
    model = ProgressionDecision
    extra = 0
    readonly_fields = ("student", "outcome", "target_class", "records_pack_ready", "note")


@admin.register(ProgressionBatch)
class ProgressionBatchAdmin(admin.ModelAdmin):
    list_display = ("school", "external_id", "from_session", "to_session", "source_class", "status", "applied_at")
    list_filter = ("status", "school")
    search_fields = ("external_id", "school__name", "source_class__name")
    inlines = (ProgressionDecisionInline,)
