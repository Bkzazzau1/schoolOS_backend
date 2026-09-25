from django.apps import AppConfig


class TransferverifyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.transferverify"
    label = "transferverify"
    verbose_name = "SchoolOS TransferVerify"

    def ready(self):
        from apps.sync import registry

        from .handlers import HANDLERS

        for handler in HANDLERS:
            registry.register(handler)
