from django.apps import AppConfig


class PayrollConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.payroll"
    label = "payroll"

    def ready(self):
        from apps.sync import registry

        from .handler import PayrollBatchHandler

        registry.register(PayrollBatchHandler())
