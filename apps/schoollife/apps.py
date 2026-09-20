from django.apps import AppConfig


class SchoolLifeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.schoollife"
    label = "schoollife"

    def ready(self):
        from apps.sync import registry

        from .community import HANDLERS as COMMUNITY
        from .framework import SchoolLifeHandler
        from .specs import SPECS

        for handler in [*COMMUNITY, *(SchoolLifeHandler(spec) for spec in SPECS)]:
            registry.register(handler)
