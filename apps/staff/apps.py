from django.apps import AppConfig


class StaffConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.staff"
    label = "staff"

    def ready(self):
        from apps.media import registry as media_registry
        from apps.sync import registry

        from .handlers import HANDLERS
        from .media_owner import owner_kind

        for handler in HANDLERS:
            registry.register(handler)
        media_registry.register(owner_kind)
