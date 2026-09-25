from django.contrib import admin

from .models import (
    AssociationAdministrator,
    BadDebtClassification,
    BadDebtEvent,
    BiometricConsent,
    NetworkSearchAudit,
    NetworkStudentIdentity,
    SchoolAssociationMembership,
    SchoolProprietorAssociation,
    StudentBiometricTemplate,
    StudentNetworkEnrollment,
    TransferAlert,
    TransferClearance,
    TransferClearanceDispute,
    TransferVerificationRequest,
)


@admin.register(BadDebtClassification)
class BadDebtClassificationAdmin(admin.ModelAdmin):
    list_display = ("external_id", "school", "student", "status", "outstanding_amount_minor", "classified_at")
    list_filter = ("status", "school")
    search_fields = ("external_id", "student__first_name", "student__surname", "student__student_code")
    readonly_fields = ("created_at", "updated_at", "classified_at", "resolved_at")


admin.site.register(BadDebtEvent)


@admin.register(SchoolProprietorAssociation)
class SchoolProprietorAssociationAdmin(admin.ModelAdmin):
    """Associations are created and verified by SchoolOS staff here - a
    school only ever joins one afterwards, through the live join-request API
    an association administrator then approves."""

    list_display = ("name", "status", "geographic_scope", "created_at")
    list_filter = ("status",)
    search_fields = ("name", "registration_reference")
    readonly_fields = ("created_at", "updated_at")


@admin.register(SchoolAssociationMembership)
class SchoolAssociationMembershipAdmin(admin.ModelAdmin):
    list_display = ("school", "association", "status", "requested_at", "decided_at")
    list_filter = ("status", "association")
    search_fields = ("school__name", "association__name")
    readonly_fields = ("requested_at", "updated_at")


@admin.register(AssociationAdministrator)
class AssociationAdministratorAdmin(admin.ModelAdmin):
    list_display = ("user", "association", "is_active", "added_at")
    list_filter = ("association", "is_active")
    search_fields = ("user__email",)
    readonly_fields = ("added_at",)


@admin.register(NetworkStudentIdentity)
class NetworkStudentIdentityAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at")
    readonly_fields = ("created_at", "updated_at")


@admin.register(StudentNetworkEnrollment)
class StudentNetworkEnrollmentAdmin(admin.ModelAdmin):
    list_display = ("school", "student", "network_identity", "guardian_phone_e164", "linked_at")
    list_filter = ("school",)
    search_fields = ("student__first_name", "student__surname", "guardian_phone_e164")
    readonly_fields = ("linked_at", "updated_at")


@admin.register(TransferAlert)
class TransferAlertAdmin(admin.ModelAdmin):
    list_display = ("id", "source_school", "state", "snapshot_status", "published_at")
    list_filter = ("state", "source_school")
    readonly_fields = ("published_at", "withdrawn_at", "resolved_at", "updated_at")


admin.site.register(NetworkSearchAudit)


@admin.register(TransferVerificationRequest)
class TransferVerificationRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "requesting_school", "status", "requested_at", "responded_at")
    list_filter = ("status",)
    search_fields = ("requesting_school__name",)
    readonly_fields = ("requested_at", "responded_at", "updated_at")


@admin.register(BiometricConsent)
class BiometricConsentAdmin(admin.ModelAdmin):
    list_display = ("student", "school", "status", "guardian_name", "recorded_at")
    list_filter = ("status", "school")
    search_fields = ("student__first_name", "student__surname", "guardian_name")
    readonly_fields = ("recorded_at", "withdrawn_at")


@admin.register(StudentBiometricTemplate)
class StudentBiometricTemplateAdmin(admin.ModelAdmin):
    """No school has real capture hardware yet (see apps.transferverify.
    biometrics' own docstring) - this exists so the data path can be
    exercised and tested ahead of that vendor decision."""

    list_display = ("network_identity", "enrolled_school", "finger", "quality_score", "captured_at", "revoked_at")
    list_filter = ("enrolled_school", "finger")
    exclude = ("template_data",)
    readonly_fields = ("captured_at",)


@admin.register(TransferClearanceDispute)
class TransferClearanceDisputeAdmin(admin.ModelAdmin):
    list_display = ("id", "transfer_alert", "reason", "status", "opened_at", "reviewed_at")
    list_filter = ("status", "reason")
    readonly_fields = ("opened_at", "reviewed_at", "updated_at")


@admin.register(TransferClearance)
class TransferClearanceAdmin(admin.ModelAdmin):
    list_display = ("id", "issuing_school", "status", "issued_at", "revoked_at")
    list_filter = ("status", "issuing_school")
    readonly_fields = ("issued_at", "revoked_at")
