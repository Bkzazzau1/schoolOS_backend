from django.apps import AppConfig


class StudentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.students"
    label = "students"
    verbose_name = "SchoolOS Students"

    def ready(self):
        from apps.sync import registry

        from .handlers import HANDLERS
        from .parent_sync import HANDLER as PARENT_FAMILY_LINK_HANDLER
        from .student_sync import HANDLER as STUDENT_CLASS_LINK_HANDLER

        for handler in [
            *HANDLERS,
            PARENT_FAMILY_LINK_HANDLER,
            STUDENT_CLASS_LINK_HANDLER,
        ]:
            registry.register(handler)

        # Register cross-domain hooks only after Django has loaded every app.
        from . import signals  # noqa: F401
