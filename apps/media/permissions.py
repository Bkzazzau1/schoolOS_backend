"""Who may act on a file. Every check goes through the OwnerKind the owning feature registered (see registry.py) -
this app never has an opinion of its own about who may see a Gallery photo or a staff document. An asset is always
first re-fetched scoped to the acting membership's own school; a wrong school id in the URL, or an asset id from
another school, is a 404, never a 403 that would confirm the id exists elsewhere.
"""

from rest_framework.exceptions import NotFound, PermissionDenied

from apps.core.permissions import require_membership

from . import registry
from .models import MediaAsset


def acting_membership(request, school_id):
    return require_membership(request.user, school_id, membership_id=request.query_params.get("membership"))


def owner_kind_for(owner_type: str) -> registry.OwnerKind:
    kind = registry.get(owner_type)
    if kind is None:
        raise NotFound("That kind of record is not known here.")
    return kind


def require_owner(membership, owner_type: str, owner_id: str) -> registry.OwnerKind:
    kind = owner_kind_for(owner_type)
    if not kind.exists(membership.school, owner_id):
        raise NotFound("That record was not found.")
    return kind


def require_view(membership, owner_type: str, owner_id: str) -> registry.OwnerKind:
    kind = require_owner(membership, owner_type, owner_id)
    if not kind.can_view(membership, owner_id):
        raise PermissionDenied("You may not see files here.")
    return kind


def require_contribute(membership, owner_type: str, owner_id: str) -> registry.OwnerKind:
    kind = require_owner(membership, owner_type, owner_id)
    if not kind.can_contribute(membership, owner_id):
        raise PermissionDenied("You may not add a file here.")
    return kind


def require_manage(membership, owner_type: str, owner_id: str, *, uploaded_by: str | None) -> registry.OwnerKind:
    kind = require_owner(membership, owner_type, owner_id)
    if not kind.can_manage(membership, owner_id, uploaded_by):
        raise PermissionDenied("You may not manage this file.")
    return kind


def get_asset(membership, asset_id) -> MediaAsset:
    """The asset, scoped to the acting membership's own school. An id from another school is indistinguishable
    from one that does not exist at all."""
    asset = MediaAsset.objects.filter(school=membership.school, id=asset_id).first()
    if asset is None:
        raise NotFound("That file was not found.")
    return asset


def uploaded_by_id(asset: MediaAsset) -> str | None:
    return str(asset.uploaded_by_id) if asset.uploaded_by_id else None
