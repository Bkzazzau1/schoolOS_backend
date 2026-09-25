from django.contrib import admin

from .models import BankAuditEvent, BankConnection, BankTransaction


@admin.register(BankConnection)
class BankConnectionAdmin(admin.ModelAdmin):
    """Read-only, and without the sealed credential: nobody should read or edit it here."""

    list_display = ("school", "provider", "account_mask", "purpose", "status", "is_sandbox", "last_synced_at")
    list_filter = ("provider", "status", "is_sandbox")
    exclude = ("sealed_credentials", "webhook_token_hash", "account_fingerprint")
    readonly_fields = [
        f.name for f in BankConnection._meta.fields if f.name not in ("sealed_credentials", "webhook_token_hash", "account_fingerprint")
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BankTransaction)
class BankTransactionAdmin(admin.ModelAdmin):
    list_display = ("school", "provider", "amount_minor", "sender_name", "reconciliation_status", "transaction_date")
    list_filter = ("provider", "reconciliation_status", "is_sandbox")

    def has_change_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BankAuditEvent)
class BankAuditEventAdmin(admin.ModelAdmin):
    list_display = ("school", "kind", "actor", "at")

    def has_change_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
