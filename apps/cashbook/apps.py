from django.apps import AppConfig


class CashbookConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.cashbook"
    label = "cashbook"

    def ready(self):
        from apps.sync import registry

        from .handlers import CashflowEntryHandler

        registry.register(CashflowEntryHandler())
