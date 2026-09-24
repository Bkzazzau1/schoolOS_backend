from django.apps import AppConfig


class AcademicsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.academics"
    label = "academics"
    verbose_name = "SchoolOS Academics"

    def ready(self):
        # The curriculum models live in a separate module to keep the original
        # session/progression model file readable. Importing them here registers
        # them with Django before any request or sync work is processed.
        from . import curriculum_models  # noqa: F401
        from apps.sync import registry

        from .handlers import HANDLERS
        from .curriculum_handlers import HANDLERS as CURRICULUM_HANDLERS

        for handler in [*HANDLERS, *CURRICULUM_HANDLERS]:
            registry.register(handler)

        from . import signals  # noqa: F401
