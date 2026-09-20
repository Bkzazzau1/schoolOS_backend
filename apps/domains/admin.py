from django.contrib import admin, messages

from . import services
from .hostnames import DomainError
from .models import SchoolDomain


@admin.register(SchoolDomain)
class SchoolDomainAdmin(admin.ModelAdmin):
    list_display = ("host", "school", "kind", "status", "is_primary", "verified_at")
    list_filter = ("kind", "status", "is_primary")
    search_fields = ("host", "school__name")
    readonly_fields = ("verification_token", "dns_instructions", "verified_at", "created_at")
    actions = ["check_dns_and_verify", "make_primary", "disable_domains"]

    @admin.display(description="To verify a custom domain")
    def dns_instructions(self, obj):
        if obj.pk is None or obj.kind != SchoolDomain.Kind.CUSTOM:
            return "Platform domains are verified automatically."
        return f"Add a TXT record named {obj.dns_record_name} with the value {obj.verification_token}."

    def _each(self, request, queryset, action, success):
        for domain in queryset:
            try:
                action(domain)
                self.message_user(request, f"{domain.host}: {success}")
            except DomainError as error:
                self.message_user(request, f"{domain.host}: {error.message}", level=messages.ERROR)

    @admin.action(description="Check DNS and verify the selected custom domains")
    def check_dns_and_verify(self, request, queryset):
        self._each(request, queryset, services.verify_custom_domain, "verified.")

    @admin.action(description="Use as the school's primary domain (for emailed links)")
    def make_primary(self, request, queryset):
        self._each(request, queryset, services.set_primary, "is now primary.")

    @admin.action(description="Disable the selected domains")
    def disable_domains(self, request, queryset):
        self._each(request, queryset, services.disable, "disabled.")
