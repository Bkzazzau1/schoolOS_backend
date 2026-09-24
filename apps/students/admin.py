from django.contrib import admin

from .models import (
    AdmissionApplication,
    GuardianLink,
    SchoolRosterRevision,
    Student,
    StudentEnrollment,
    StudentLifecycleEvent,
    StudentRegistration,
)


class _CanonicalReadOnlyAdmin(admin.ModelAdmin):
    """Canonical roster changes go through SchoolOS workflows, not raw admin edits."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AdmissionApplication)
class AdmissionApplicationAdmin(_CanonicalReadOnlyAdmin):
    list_display = ("reference", "applicant_name", "school", "stage", "updated_at")
    list_filter = ("school", "stage", "source")
    search_fields = ("reference", "applicant_name", "guardian_name", "guardian_phone")


@admin.register(Student)
class StudentAdmin(_CanonicalReadOnlyAdmin):
    list_display = ("student_code", "admission_number", "full_name_display", "school", "status")
    list_filter = ("school", "status")
    search_fields = ("student_code", "admission_number", "first_name", "surname", "other_name")

    @admin.display(description="Student")
    def full_name_display(self, obj):
        return obj.full_name


@admin.register(StudentRegistration)
class StudentRegistrationAdmin(_CanonicalReadOnlyAdmin):
    list_display = ("registration_id", "student_code", "school", "status", "updated_at")
    list_filter = ("school", "status", "academic_section")
    search_fields = ("registration_id", "student_code", "admission_number", "first_name", "surname")


@admin.register(GuardianLink)
class GuardianLinkAdmin(_CanonicalReadOnlyAdmin):
    list_display = ("student", "name", "relationship", "phone", "is_primary")
    search_fields = ("student__student_code", "name", "phone", "email")


@admin.register(StudentEnrollment)
class StudentEnrollmentAdmin(_CanonicalReadOnlyAdmin):
    list_display = ("student", "class_name", "academic_section", "status", "is_billable", "started_at", "ended_at")
    list_filter = ("school", "status", "is_billable", "academic_section")
    search_fields = ("student__student_code", "student__admission_number", "class_name")


@admin.register(StudentLifecycleEvent)
class StudentLifecycleEventAdmin(_CanonicalReadOnlyAdmin):
    list_display = ("external_id", "student", "workflow", "status", "requested_at", "completed_at")
    list_filter = ("school", "workflow", "status")
    search_fields = ("external_id", "student__student_code", "student__admission_number")


@admin.register(SchoolRosterRevision)
class SchoolRosterRevisionAdmin(_CanonicalReadOnlyAdmin):
    list_display = ("school", "revision", "updated_at")
    search_fields = ("school__name",)
