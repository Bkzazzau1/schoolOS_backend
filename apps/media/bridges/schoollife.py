"""Lets a school-life module (Gallery, Community, ...) become a media owner without the media app knowing what a
Gallery album is, and without duplicating who-may-see/who-may-change rules that `apps/schoollife/framework.py`'s
`Spec`/`SchoolLifeHandler` already enforce for the record itself. The record's own sync entity - already registered
with `apps/sync/registry.py` by `apps/schoollife/apps.py` - is the single source of truth for those rules; this
module only asks it the same two questions the sync layer already knows how to answer: "can this membership write
this kind of record at all" and "may this membership see this stored payload".

Used for any module built on that shared Spec engine - Gallery today; ready for Community, excursions and
incident evidence the same way, the day each is asked to carry real attachments.
"""

from apps.media.registry import OwnerKind
from apps.sync import registry as sync_registry
from apps.sync.models import SyncRecord


def _record(school, entity_type: str, owner_id: str):
    return SyncRecord.objects.filter(school=school, entity_type=entity_type, entity_id=owner_id, deleted=False).first()


def owner_kind_for_sync_entity(entity_type: str) -> OwnerKind:
    def exists(school, owner_id: str) -> bool:
        return _record(school, entity_type, owner_id) is not None

    def can_view(membership, owner_id: str) -> bool:
        record = _record(membership.school, entity_type, owner_id)
        handler = sync_registry.get(entity_type)
        if record is None or handler is None:
            return False
        return handler.visible(membership, record.payload) is not None

    def can_contribute(membership, owner_id: str) -> bool:
        record = _record(membership.school, entity_type, owner_id)
        handler = sync_registry.get(entity_type)
        if record is None or handler is None:
            return False
        return membership.role in handler.roles

    def can_manage(membership, owner_id: str, uploaded_by: str | None) -> bool:
        record = _record(membership.school, entity_type, owner_id)
        handler = sync_registry.get(entity_type)
        if record is None or handler is None:
            return False
        # A module built on Spec exposes the finer manage/contribute split; a hand-rolled EntityHandler (like
        # Community's own) only exposes its combined `roles` - the same set `authorize()` itself checks, so
        # falling back to it is not a widening of what that module already allows.
        manage_roles = handler.spec.manage if hasattr(handler, "spec") else handler.roles
        if membership.role in manage_roles:
            return True
        return uploaded_by is not None and uploaded_by == str(membership.id)

    return OwnerKind(key=entity_type, exists=exists, can_view=can_view, can_contribute=can_contribute, can_manage=can_manage)
