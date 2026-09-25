from django.apps import AppConfig


class CbtConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.cbt"
    label = "cbt"
    verbose_name = "Computer-Based Testing"

    def ready(self):
        from apps.sync import registry

        from .handlers import HANDLERS

        for handler in HANDLERS:
            registry.register(handler)
