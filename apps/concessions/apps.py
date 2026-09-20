from django.apps import AppConfig


class ConcessionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.concessions"
    label = "concessions"

    def ready(self):
        from apps.sync import registry

        from .handler import ConcessionHandler

        registry.register(ConcessionHandler())
