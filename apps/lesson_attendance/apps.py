from django.apps import AppConfig


class LessonAttendanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.lesson_attendance"
    label = "lesson_attendance"
    verbose_name = "Lesson Attendance"

    def ready(self):
        from apps.sync import registry

        from .handlers import HANDLERS

        for handler in HANDLERS:
            registry.register(handler)

        from . import signals  # noqa: F401
