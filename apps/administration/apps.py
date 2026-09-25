from django.apps import AppConfig


class AdministrationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.administration"
    label = "administration"

    def ready(self):
        from apps.schoollife.framework import SchoolLifeHandler
        from apps.sync import registry

        from .specs import SPECS

        for spec in SPECS:
            registry.register(SchoolLifeHandler(spec))
