from django.apps import AppConfig


class SchoolStoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.schoolstore"
    label = "schoolstore"

    def ready(self):
        from apps.sync import registry

        from .handlers import StoreItemHandler, StoreSaleHandler

        registry.register(StoreItemHandler())
        registry.register(StoreSaleHandler())
