from django.apps import AppConfig


class ReportCardsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.report_cards"
    label = "report_cards"
    verbose_name = "Report Cards"

    def ready(self):
        from apps.sync import registry

        from .handlers import HANDLERS

        for handler in HANDLERS:
            registry.register(handler)

        from . import signals  # noqa: F401
