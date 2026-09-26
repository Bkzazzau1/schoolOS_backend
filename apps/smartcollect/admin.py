from django.contrib import admin

from .models import (
    CollectionAuditEvent,
    CollectionBatchEvent,
    CollectionGenerationBatch,
    CollectionGenerationBatchItem,
    CollectionPolicyOverride,
    ProviderJob,
    ProviderSwitch,
    SchoolCollectionPolicy,
)


class ReadOnly(admin.ModelAdmin):
    """Smart Money Collection records are history: they are read here, never edited or deleted."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SchoolCollectionPolicy)
class PolicyAdmin(ReadOnly):
    list_display = ("school", "default_account_mode", "default_settlement_action", "default_arrears_policy", "updated_at")


@admin.register(CollectionGenerationBatch)
class BatchAdmin(ReadOnly):
    list_display = ("id", "school", "provider", "status", "version", "selected_count", "prepared_at")
    list_filter = ("status", "provider")


@admin.register(CollectionGenerationBatchItem)
class ItemAdmin(ReadOnly):
    list_display = ("batch", "family", "selected", "eligibility_status", "generation_status", "attempt_count")
    list_filter = ("generation_status", "eligibility_status")


@admin.register(ProviderJob)
class JobAdmin(ReadOnly):
    list_display = ("kind", "status", "attempts", "run_after", "last_error_code")
    list_filter = ("kind", "status")


@admin.register(ProviderSwitch)
class SwitchAdmin(ReadOnly):
    list_display = ("school", "status", "scheduled_for", "applied_at")


for model in (CollectionPolicyOverride, CollectionAuditEvent, CollectionBatchEvent):
    admin.site.register(model, ReadOnly)
