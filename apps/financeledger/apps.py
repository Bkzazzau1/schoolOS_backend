from django.apps import AppConfig


class FinanceLedgerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.financeledger"
    label = "financeledger"

    def ready(self):
        from apps.sync import registry

        from .handlers import BankLineHandler, FeeStructureHandler, PaymentHandler, ReminderHandler, TermDueHandler

        registry.register(FeeStructureHandler())
        registry.register(TermDueHandler())
        registry.register(PaymentHandler())
        registry.register(ReminderHandler())
        registry.register(BankLineHandler())
