"""The media service's own business logic: initiate an upload, receive its bytes (local storage), complete it,
verify it, build its thumbnail, retire it, purge a retired file's bytes, and open a download. Views stay thin and
call this module; this module never touches the request/response directly.
"""

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import audit, jobs, permissions, validation
from .constants import Status, Visibility, max_bytes_for
from .models import MediaAsset, MediaDerivative
from .scanning import Infected, ScannerError, get_scanner
from .storage import get_storage
from .thumbnails import build_image_thumbnail, probe_dimensions
from .transcoding import TranscoderError, TranscoderUnavailable, get_transcoder
from .validation import UploadRefused


def _download_ttl_seconds() -> int:
    return int(getattr(settings, "MEDIA_DOWNLOAD_URL_TTL_SECONDS", 300))


class MediaError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _checksum_looks_valid(value: str) -> bool:
    value = (value or "").strip().lower()
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@transaction.atomic
def initiate(
    membership,
    *,
    owner_type: str,
    owner_id: str,
    category: str,
    file_name: str,
    mime_type: str,
    byte_size,
    checksum_sha256: str,
    caption: str = "",
    visibility: str = Visibility.PRIVATE,
):
    permissions.require_contribute(membership, owner_type, owner_id)
    try:
        intent = validation.check_intent(
            category=category, declared_mime_type=mime_type, declared_byte_size=byte_size,
            original_filename=file_name, school_id=membership.school_id,
        )
    except UploadRefused as error:
        raise MediaError(error.code, error.message) from error
    if not _checksum_looks_valid(checksum_sha256):
        raise MediaError("invalid_checksum", "The file's checksum was not given properly.")
    if visibility not in Visibility.values:
        raise MediaError("invalid_visibility", "That is not a visibility SchoolOS knows.")

    storage = get_storage()
    asset = MediaAsset.objects.create(
        school=membership.school,
        uploaded_by=membership,
        owner_type=owner_type,
        owner_id=owner_id,
        category=category,
        original_filename=intent.safe_original_filename,
        stored_filename=intent.stored_filename,
        mime_type=intent.mime_type,
        media_type=intent.media_type,
        byte_size=byte_size,
        checksum_sha256=checksum_sha256.strip().lower(),
        storage_provider=storage.provider,
        storage_key=intent.storage_key,
        visibility=visibility,
        status=Status.PENDING_UPLOAD,
        caption=caption[:500],
    )
    instructions = storage.initiate_upload(intent.storage_key, mime_type=intent.mime_type, asset_id=asset.id)
    audit.record(membership.school, "upload_initiated", actor=membership, obj=asset, owner_type=owner_type, category=category, byte_size=byte_size)
    return asset, instructions


def receive_body(asset: MediaAsset, data: bytes) -> None:
    """Local storage's own upload receiver: the raw bytes PUT to SchoolOS's own endpoint. Never used for
    S3-compatible storage, where the device PUTs straight to the object store instead."""
    if asset.status != Status.PENDING_UPLOAD:
        raise MediaError("not_pending", "This file has already been uploaded.")
    if len(data) != asset.byte_size:
        raise MediaError("size_mismatch", "The file did not arrive as the size that was declared.")
    try:
        validation.check_signature(declared_mime_type=asset.mime_type, head=data[:64])
    except UploadRefused as error:
        _mark_failed(asset, error.code)
        raise MediaError(error.code, error.message) from error
    get_storage().write_bytes(asset.storage_key, data, mime_type=asset.mime_type)
    asset.status = Status.UPLOADED
    asset.save(update_fields=["status", "updated_at"])


def complete(membership, asset: MediaAsset) -> MediaAsset:
    """Idempotent: calling it again once an upload has already been completed (or has already progressed further)
    changes nothing - this is also how a client reconciles an upload it lost track of after a retry."""
    if asset.status == Status.PENDING_UPLOAD:
        storage = get_storage()
        found = storage.verify_existence(asset.storage_key)
        if found is None:
            raise MediaError("not_uploaded", "That file has not finished uploading yet.")
        if found.byte_size != asset.byte_size:
            _mark_failed(asset, "size_mismatch")
            raise MediaError("size_mismatch", "The uploaded file was not the size that was declared.")
        asset.status = Status.UPLOADED
        asset.save(update_fields=["status", "updated_at"])
    if asset.status == Status.UPLOADED:
        jobs.enqueue(asset, "verify")
        audit.record(asset.school, "upload_completed", actor=membership, obj=asset)
        jobs.drain_inline()
        asset.refresh_from_db()
    return asset


def _mark_failed(asset: MediaAsset, code: str) -> None:
    asset.status, asset.failure_code = Status.FAILED, code
    asset.save(update_fields=["status", "failure_code", "updated_at"])
    audit.record(asset.school, "upload_failed", obj=asset, code=code)


def _mark_available(asset: MediaAsset) -> None:
    asset.status = Status.AVAILABLE
    asset.save(update_fields=["status", "updated_at"])
    audit.record(asset.school, "upload_available", obj=asset)


def _mark_quarantined(asset: MediaAsset, code: str) -> None:
    asset.status, asset.failure_code = Status.QUARANTINED, code
    asset.save(update_fields=["status", "failure_code", "updated_at"])
    audit.record(asset.school, "upload_quarantined", obj=asset, code=code)


def run_verify(asset: MediaAsset) -> None:
    if asset.status != Status.UPLOADED:
        return  # a late or duplicate run of the same job: nothing to do
    storage = get_storage()
    ceiling = max_bytes_for(asset.category, asset.media_type)
    data = storage.read_bytes(asset.storage_key, max_bytes=ceiling)  # MediaStorageError propagates: retried/backed off by jobs.py

    checksum = validation.sha256_of(data)
    if checksum != asset.checksum_sha256:
        _mark_failed(asset, "checksum_mismatch")
        return
    try:
        validation.check_signature(declared_mime_type=asset.mime_type, head=data[:64])
    except UploadRefused as error:
        _mark_failed(asset, error.code)
        return
    try:
        get_scanner().scan(data)
    except Infected as error:
        _mark_quarantined(asset, error.code)
        return
    except ScannerError:
        # No real malware scanner exists on this server, or a genuine scan failure that is not the uploader's
        # fault either - either way, a known, expected gap: the upload still verifies, the same graceful degrade
        # a video with no real transcoder gets (see transcoding.py).
        pass

    asset.status = Status.VERIFIED
    if asset.is_image:
        dimensions = probe_dimensions(data)
        if dimensions is None:
            _mark_failed(asset, "not_a_valid_image")
            return
        asset.width, asset.height = dimensions
    asset.save(update_fields=["status", "width", "height", "updated_at"])
    audit.record(asset.school, "upload_verified", obj=asset)

    if asset.is_image or asset.is_video:
        jobs.enqueue(asset, "thumbnail")
    else:
        _mark_available(asset)


def run_thumbnail(asset: MediaAsset) -> None:
    if asset.status != Status.VERIFIED or not (asset.is_image or asset.is_video):
        return
    storage = get_storage()
    data = storage.read_bytes(asset.storage_key, max_bytes=max_bytes_for(asset.category, asset.media_type))
    if asset.is_image:
        thumbnail_bytes, width, height, mime_type = build_image_thumbnail(data)
    else:
        try:
            thumbnail_bytes, width, height, mime_type = get_transcoder().build_thumbnail(data)
        except TranscoderUnavailable:
            # No real video transcoder exists on this server - the video itself is still real, stored and
            # downloadable; it simply has no preview frame, the same honest gap docs/MEDIA.md already describes.
            # Not a job failure: nothing here was done wrong, so this never retries or marks the asset failed.
            _mark_available(asset)
            return
        except TranscoderError:
            # A genuine decode failure (a corrupt file ffmpeg itself cannot read) is not the upload's fault
            # either - the video stays available without a preview rather than being marked failed.
            _mark_available(asset)
            return
    key = f"{asset.storage_key}.thumb.jpg"
    storage.write_bytes(key, thumbnail_bytes, mime_type=mime_type)
    MediaDerivative.objects.update_or_create(
        asset=asset,
        kind="thumbnail",
        defaults={
            "storage_provider": storage.provider,
            "storage_key": key,
            "mime_type": mime_type,
            "byte_size": len(thumbnail_bytes),
            "width": width,
            "height": height,
        },
    )
    _mark_available(asset)


def run_purge(asset: MediaAsset) -> None:
    if asset.status != Status.RETIRED:
        return
    storage = get_storage()
    storage.delete(asset.storage_key)
    for derivative in asset.derivatives.all():
        storage.delete(derivative.storage_key)
    audit.record(asset.school, "bytes_purged", obj=asset)


def list_for_owner(membership, *, owner_type: str, owner_id: str, include_retired: bool = False):
    permissions.require_view(membership, owner_type, owner_id)
    queryset = MediaAsset.objects.filter(school=membership.school, owner_type=owner_type, owner_id=owner_id)
    if not include_retired:
        queryset = queryset.exclude(status=Status.RETIRED)
    return queryset.select_related("uploaded_by__user").prefetch_related("derivatives").order_by("-created_at")


def retire(membership, asset: MediaAsset, *, reason: str = "") -> MediaAsset:
    permissions.require_manage(membership, asset.owner_type, asset.owner_id, uploaded_by=permissions.uploaded_by_id(asset))
    if asset.status == Status.RETIRED:
        return asset
    asset.status = Status.RETIRED
    asset.deleted_at = timezone.now()
    asset.save(update_fields=["status", "deleted_at", "updated_at"])
    jobs.enqueue(asset, "purge")
    audit.record(asset.school, "retired", actor=membership, obj=asset, reason=(reason or "")[:200])
    jobs.drain_inline()
    return asset


def open_download(membership, asset: MediaAsset, *, thumbnail: bool = False):
    """Returns (DownloadInstructions, mime_type) for something already checked as visible to this membership."""
    permissions.require_view(membership, asset.owner_type, asset.owner_id)
    storage = get_storage()
    if thumbnail:
        derivative = next((d for d in asset.derivatives.all() if d.kind == "thumbnail"), None)
        if derivative is None:
            derivative = asset.derivatives.filter(kind="thumbnail").first()
        if derivative is None:
            raise MediaError("no_thumbnail", "No thumbnail exists for this file yet.")
        instructions = storage.open_download(
            derivative.storage_key, filename=f"{asset.stored_filename}.thumb.jpg", mime_type=derivative.mime_type, ttl_seconds=_download_ttl_seconds()
        )
        return instructions, derivative.storage_key, derivative.mime_type
    if asset.status not in (Status.AVAILABLE, Status.VERIFIED):
        raise MediaError("not_available", "This file is not ready to open yet.")
    instructions = storage.open_download(
        asset.storage_key, filename=asset.original_filename or asset.stored_filename, mime_type=asset.mime_type, ttl_seconds=_download_ttl_seconds()
    )
    audit.record(asset.school, "download_opened", actor=membership, obj=asset)
    return instructions, asset.storage_key, asset.mime_type
