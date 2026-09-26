from django.contrib import admin

from .models import (
    DirectDebitMandate,
    MandateAuditEvent,
    MandateConsent,
    MandateDebitBatch,
    MandateDebitInstruction,
    MandateEvent,
    MandateProviderConnection,
    MandateProviderJob,
    MandateTransaction,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    """The trail is read-only here: nothing about a mandate or a debit is edited or deleted through the admin, and no secret is ever shown."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MandateProviderConnection)
class MandateProviderConnectionAdmin(ReadOnlyAdmin):
    list_display = ("school", "provider", "environment", "status", "webhook_status", "last_verified_at")
    exclude = ("sealed_credentials",)


@admin.register(DirectDebitMandate)
class DirectDebitMandateAdmin(ReadOnlyAdmin):
    list_display = ("school", "family", "provider", "bank_name", "account_mask", "status", "is_primary", "created_at")
    exclude = ("sealed_account_details", "account_fingerprint")


@admin.register(MandateDebitBatch)
class MandateDebitBatchAdmin(ReadOnlyAdmin):
    list_display = ("school", "title", "status", "total_items", "total_amount_minor", "prepared_at")


@admin.register(MandateDebitInstruction)
class MandateDebitInstructionAdmin(ReadOnlyAdmin):
    list_display = ("batch", "family", "provider", "status", "proposed_debit_minor")


@admin.register(MandateTransaction)
class MandateTransactionAdmin(ReadOnlyAdmin):
    list_display = ("school", "family", "provider", "status", "amount_minor", "created_at")


for model in (MandateAuditEvent, MandateConsent, MandateEvent, MandateProviderJob):
    admin.site.register(model, ReadOnlyAdmin)
