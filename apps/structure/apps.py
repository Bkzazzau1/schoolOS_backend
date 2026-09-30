from django.apps import AppConfig


class StructureConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.structure"
    label = "structure"

    def ready(self):
        from apps.media import registry as media_registry
        from apps.media.bridges.schoollife import owner_kind_for_sync_entity
        from apps.sync import registry

        from .constants import APPEARANCE
        from .handlers import HANDLERS

        for handler in HANDLERS:
            registry.register(handler)

        # The school's own logo can now also keep a real, durable copy in the shared media service, alongside
        # (never instead of) the small base64 copy inside this same record's own sync payload - that copy stays
        # what every screen renders from, in every mode including fully offline, so a school never loses its
        # logo just because no server exists. Reuses the same generic bridge every other owner kind does; only
        # the proprietor may contribute or manage a file here, matching this record's own write rule exactly.
        media_registry.register(owner_kind_for_sync_entity(APPEARANCE))
