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
        from .specs import SPECS
        from .specs.campus import GALLERY

        for handler in [*COMMUNITY, *(SchoolLifeHandler(spec) for spec in SPECS)]:
            registry.register(handler)

        # Gallery is the first real consumer of the shared media service (real photos and videos, not just an
        # album's metadata); Community is registered ready for the day its own "mediaLabel" caption is replaced
        # by a real attachment, without either module's own access rules being duplicated - see the bridge.
        for entity_type in (GALLERY.entity_type, COMMUNITY_POST):
            media_registry.register(owner_kind_for_sync_entity(entity_type))
