from django.apps import AppConfig


class TimetableConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.timetable"
    label = "timetable"
    verbose_name = "SchoolOS Timetable"

    def ready(self):
        from apps.sync import registry

        from .handlers import HANDLERS

        for handler in HANDLERS:
            registry.register(handler)

        from . import signals  # noqa: F401
