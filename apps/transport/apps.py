from django.apps import AppConfig


class TransportConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.transport"
    label = "transport"

    def ready(self):
        from apps.sync import registry

        from .handlers import HANDLERS

        for handler in HANDLERS:
            registry.register(handler)
