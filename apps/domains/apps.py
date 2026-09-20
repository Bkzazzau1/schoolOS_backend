from django.apps import AppConfig


class DomainsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.domains"
    label = "domains"

    def ready(self):
        from . import signals  # noqa: F401  (connects the handler)
