"""What the API sends back - plain functions returning dicts, the same style as apps/mandates/serializers.py.
camelCase throughout, to match every other SchoolOS endpoint the app already reads. A credential, a storage key or
a raw filesystem path never appears in any of these."""


def asset(a) -> dict:
    return {
        "id": str(a.id),
        "ownerType": a.owner_type,
        "ownerId": a.owner_id,
        "category": a.category,
        "fileName": a.original_filename,
        "mimeType": a.mime_type,
        "mediaType": a.media_type,
        "byteSize": a.byte_size,
        "visibility": a.visibility,
        "status": a.status,
        "failureCode": a.failure_code,
        "caption": a.caption,
        "width": a.width,
        "height": a.height,
        "hasThumbnail": _has_thumbnail(a),
        "uploadedByMembershipId": str(a.uploaded_by_id) if a.uploaded_by_id else None,
        "createdAt": a.created_at.isoformat(),
        "updatedAt": a.updated_at.isoformat(),
        "version": a.version,
    }


def _has_thumbnail(a) -> bool:
    prefetched = getattr(a, "_prefetched_objects_cache", {}) or {}
    if "derivatives" in prefetched:
        return any(d.kind == "thumbnail" for d in prefetched["derivatives"])
    return a.derivatives.filter(kind="thumbnail").exists()


def upload_instructions(instructions) -> dict:
    return {
        "mode": instructions.mode,
        "uploadUrl": instructions.upload_url,
        "method": instructions.method,
        "headers": instructions.headers,
        "expiresAt": instructions.expires_at.isoformat() if instructions.expires_at else None,
    }


def download(instructions, mime_type: str) -> dict:
    return {
        "mode": instructions.mode,
        "url": instructions.url,
        "mimeType": mime_type,
        "expiresAt": instructions.expires_at.isoformat() if instructions.expires_at else None,
    }


def audit_event(e) -> dict:
    return {
        "id": e.id,
        "kind": e.kind,
        "objectType": e.object_type,
        "objectId": e.object_id,
        "detail": e.detail,
        "actorMembershipId": str(e.actor_id) if e.actor_id else None,
        "at": e.at.isoformat(),
    }
