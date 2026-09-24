from django.apps import AppConfig


class LessonDeliveryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.lesson_delivery"
    label = "lesson_delivery"
    verbose_name = "Lesson Delivery"

    def ready(self):
        from apps.sync import registry

        from .handlers import HANDLERS

        for handler in HANDLERS:
            registry.register(handler)

        from . import signals  # noqa: F401
