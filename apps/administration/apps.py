from django.apps import AppConfig


class AdministrationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.administration"
    label = "administration"

    def ready(self):
        from apps.media import registry as media_registry
        from apps.media.bridges.schoollife import owner_kind_for_sync_entity
        from apps.schoollife.framework import SchoolLifeHandler
        from apps.sync import registry

        from .specs import DOCUMENT_RECORDS, SPECS

        for spec in SPECS:
            registry.register(SchoolLifeHandler(spec))

        # A document record already tracked here (its own "document" field is a label a person types, per the
        # brief) can now reference a real uploaded file, without this record's own workflow changing: uploading a
        # file never sets its status by itself - see docs/MEDIA.md.
        media_registry.register(owner_kind_for_sync_entity(DOCUMENT_RECORDS.entity_type))
