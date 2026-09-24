from django.apps import AppConfig


class ClassTeachersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.class_teachers"
    label = "class_teachers"
    verbose_name = "Class Teachers"

    def ready(self):
        from apps.sync import registry

        from .handlers import HANDLERS

        for handler in HANDLERS:
            registry.register(handler)
