from django.apps import AppConfig


class WeeklyLearningConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.weekly_learning"
    label = "weekly_learning"
    verbose_name = "Weekly Learning"

    def ready(self):
        from apps.sync import registry

        from .handlers import HANDLERS

        for handler in HANDLERS:
            registry.register(handler)

        from . import signals  # noqa: F401
