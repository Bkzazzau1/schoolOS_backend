from django.apps import AppConfig


class SchoolLifeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.schoollife"
    label = "schoollife"

    def ready(self):
        from apps.media import registry as media_registry
        from apps.media.bridges.schoollife import owner_kind_for_sync_entity
        from apps.sync import registry

        from .community import HANDLERS as COMMUNITY
        from .community.common import POST as COMMUNITY_POST
        from .framework import SchoolLifeHandler
        from .messaging import HANDLERS as MESSAGING
        from .specs import SPECS
        from .specs.calendar import EXCURSIONS
        from .specs.campus import GALLERY

        for handler in [*COMMUNITY, *MESSAGING, *(SchoolLifeHandler(spec) for spec in SPECS)]:
            registry.register(handler)

        # Gallery is the first real consumer of the shared media service (real photos and videos, not just an
        # album's metadata); Community and excursion evidence are registered ready for the day each module's own
        # placeholder (Community's "mediaLabel" caption; excursions' own lack of one) is replaced by a real
        # attachment, without any module's own access rules being duplicated - see the bridge.
        for entity_type in (GALLERY.entity_type, COMMUNITY_POST, EXCURSIONS.entity_type):
            media_registry.register(owner_kind_for_sync_entity(entity_type))
