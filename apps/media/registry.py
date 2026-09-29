"""Where a feature tells the media service what an "owner" of a file actually is.

A `MediaAsset` belongs to some other record - a Gallery album, a staff member's profile, a Community post - named
generically as `(ownerType, ownerId)` so the media app never has to know about Gallery or Community. Each owning
feature registers an `OwnerKind` for the owner types it introduces, in its own `AppConfig.ready()`, exactly the way
`apps/sync/registry.py` already asks features to register their own entity handlers. The media app itself defines
no owner kinds and imports no feature app.

The server always decides, from the owner kind's own rules, whether the acting membership may act on a given
owner - a visibility string on the asset is never trusted on its own (see constants.Visibility).
"""

from dataclasses import dataclass
from typing import Callable

from apps.schools.models import Membership, School


@dataclass(frozen=True)
class OwnerKind:
    #: The `ownerType` string the app and the API use, e.g. "gallery_media_album".
    key: str

    #: Whether `owner_id` is really a record belonging to `school` at all. Checked before anything else: a media
    #: call naming an owner that does not exist in the acting membership's own school is refused as not found,
    #: never revealing whether that id exists in a different school.
    exists: Callable[[School, str], bool]

    #: May this membership see files attached to this owner (list them, read their metadata, download them)?
    can_view: Callable[[Membership, str], bool]

    #: May this membership attach a new file to this owner?
    can_contribute: Callable[[Membership, str], bool]

    #: May this membership retire (or otherwise manage) a file already attached to this owner? `uploaded_by` is
    #: the membership id (a string, possibly None) that uploaded the file in question, so an owner kind can allow
    #: "anyone who manages this owner, or whoever added this one file themselves" without the media app knowing
    #: what "manages" means for that owner.
    can_manage: Callable[[Membership, str, str | None], bool]


_registry: dict[str, OwnerKind] = {}


def register(owner_kind: OwnerKind) -> None:
    if owner_kind.key in _registry:
        raise ValueError(f"{owner_kind.key} already has a registered owner kind.")
    _registry[owner_kind.key] = owner_kind


def get(owner_type: str) -> OwnerKind | None:
    return _registry.get(owner_type)


def registered_owner_types() -> set[str]:
    return set(_registry)
