from django.contrib import admin

from .models import (
    AcademicClass,
    AcademicSession,
    AcademicTerm,
    EnrollmentAcademicContext,
    ProgressionBatch,
    ProgressionDecision,
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
