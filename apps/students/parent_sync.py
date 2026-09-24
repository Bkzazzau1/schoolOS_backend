from apps.core.errors import Rejected
from apps.schools.models import Role
from apps.sync.registry import EntityHandler


PARENT_FAMILY_LINK_ENTITY = "parent_family_link"


class ParentFamilyLinkHandler(EntityHandler):
    """Read-only, server-generated list of children linked to one Parent membership."""

    entity_type = PARENT_FAMILY_LINK_ENTITY
    roles = frozenset()

    def authorize(self, ctx) -> None:
        raise Rejected("Parent family links are managed by the SchoolOS server.")

    def clean(self, ctx):
        raise Rejected("Parent family links are managed by the SchoolOS server.")

    def visible(self, membership, payload):
        if membership.role != Role.PARENT:
            return None
        if payload.get("parentMembershipId") != str(membership.id):
            return None
        return payload


HANDLER = ParentFamilyLinkHandler()
