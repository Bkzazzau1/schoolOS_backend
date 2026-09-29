"""Lets a staff member's onboarding documents (the "documents" list inside their `owner_staff_profile` record -
apps/staff/profiles/sections.py) carry a real uploaded file, without changing that record's own status lifecycle
(requested/received/verified) or who may act on it. Uploading a file never sets a document's status itself - a
manager still marks it received or verified - see profiles/sections.py:documents().

Mirrors apps/staff/profiles/handler.py's own rules exactly rather than duplicating a different version of them:
the owner and principal (and, separately, an administrator for VIEWING only - the file may be sensitive, but an
administrator already reviews the whole record) may see every document; only the owner, the principal, or the
staff member themselves - once their own account is linked - may add or remove one.
"""

from apps.media.registry import OwnerKind
from apps.sync.models import SyncRecord

from .constants import EDITOR_ROLES, INVITER_ROLES, PROFILE


def _profile(school, staff_id: str):
    return SyncRecord.objects.filter(school=school, entity_type=PROFILE, entity_id=staff_id, deleted=False).first()


def _is_self(membership, record) -> bool:
    return (record.payload or {}).get("linkedMembershipId") == str(membership.id)


def exists(school, staff_id: str) -> bool:
    return _profile(school, staff_id) is not None


def can_view(membership, staff_id: str) -> bool:
    record = _profile(membership.school, staff_id)
    return record is not None and (membership.role in INVITER_ROLES or _is_self(membership, record))


def can_contribute(membership, staff_id: str) -> bool:
    record = _profile(membership.school, staff_id)
    return record is not None and (membership.role in EDITOR_ROLES or _is_self(membership, record))


def can_manage(membership, staff_id: str, uploaded_by: str | None) -> bool:
    return can_contribute(membership, staff_id)


owner_kind = OwnerKind(
    key="staff_profile_document", exists=exists, can_view=can_view, can_contribute=can_contribute, can_manage=can_manage
)
