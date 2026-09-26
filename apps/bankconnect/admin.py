from django.contrib import admin

from .models import BankAuditEvent, BankTransaction, CollectionProviderConnection


@admin.register(CollectionProviderConnection)
class CollectionProviderConnectionAdmin(admin.ModelAdmin):
    """Read-only, and without the sealed credential: nobody - not a SchoolOS operator either - should read or edit a school's provider
    secret here."""

    list_display = ("school", "provider", "environment", "status", "is_active_provider", "webhook_status", "last_verified_at")
    list_filter = ("provider", "status", "environment", "is_active_provider")
    exclude = ("sealed_credentials", "webhook_token_hash", "account_fingerprint")
    readonly_fields = [
        f.name for f in CollectionProviderConnection._meta.fields if f.name not in ("sealed_credentials", "webhook_token_hash", "account_fingerprint")
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
