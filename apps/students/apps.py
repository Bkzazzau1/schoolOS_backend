from django.apps import AppConfig


class StudentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.students"
    label = "students"
    verbose_name = "SchoolOS Students"

    def ready(self):
        from apps.sync import registry

        from .handlers import HANDLERS

        for handler in HANDLERS:
            registry.register(handler)
