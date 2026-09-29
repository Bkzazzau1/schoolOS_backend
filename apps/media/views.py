from dataclasses import replace

from django.http import FileResponse, Http404
from rest_framework import status as http_status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from . import permissions, serializers, uploads
from .constants import Visibility
from .models import MediaAuditEvent
from .storage import get_storage
from .storage.local import LocalMediaStorage
from .uploads import MediaError

#: A hard ceiling on a body PUT straight to this app (local storage only - see storage/local.py's module
#: docstring). Well above the largest category SchoolOS accepts, purely to stop an oversized request being
#: buffered into memory before `receive_body` gets a chance to compare it against the declared size.
_MAX_DIRECT_BODY_BYTES = 220 * 1024 * 1024


def _refused(error: MediaError) -> Response:
    return Response({"code": error.code, "message": error.message}, status=http_status.HTTP_400_BAD_REQUEST)


class AssetsView(APIView):
    """GET lists what is attached to one owner; POST starts a new upload for it."""

    def get(self, request, school_id):
        membership = permissions.acting_membership(request, school_id)
        owner_type = request.query_params.get("ownerType", "")
        owner_id = request.query_params.get("ownerId", "")
        if not owner_type or not owner_id:
            return Response({"code": "owner_required", "message": "ownerType and ownerId are both required."}, status=400)
        assets = uploads.list_for_owner(membership, owner_type=owner_type, owner_id=owner_id)
        return Response({"assets": [serializers.asset(a) for a in assets]})

    def post(self, request, school_id):
        membership = permissions.acting_membership(request, school_id)
        data = request.data
        try:
            asset, instructions = uploads.initiate(
                membership,
                owner_type=data.get("ownerType", ""),
                owner_id=data.get("ownerId", ""),
                category=data.get("category", ""),
                file_name=data.get("fileName", ""),
                mime_type=data.get("mimeType", ""),
                byte_size=data.get("byteSize"),
                checksum_sha256=data.get("sha256", ""),
                caption=data.get("caption") or "",
                visibility=data.get("visibility") or Visibility.PRIVATE,
            )
        except MediaError as error:
            return _refused(error)
        return Response(
            {"asset": serializers.asset(asset), "upload": serializers.upload_instructions(instructions)},
            status=http_status.HTTP_201_CREATED,
        )


class AssetDetailView(APIView):
    def get(self, request, school_id, asset_id):
        membership = permissions.acting_membership(request, school_id)
        asset = permissions.get_asset(membership, asset_id)
        permissions.require_view(membership, asset.owner_type, asset.owner_id)
        return Response({"asset": serializers.asset(asset)})


class UploadBodyView(APIView):
    """PUT the file's raw bytes here - local storage only. Only the person who started the upload may send its
    bytes, and only while it is still waiting for them."""

    def put(self, request, school_id, asset_id):
        membership = permissions.acting_membership(request, school_id)
        asset = permissions.get_asset(membership, asset_id)
        permissions.require_contribute(membership, asset.owner_type, asset.owner_id)
        if permissions.uploaded_by_id(asset) != str(membership.id):
            raise PermissionDenied("Only the person who started this upload may send its bytes.")
        declared = request.META.get("CONTENT_LENGTH")
        if declared and int(declared) > _MAX_DIRECT_BODY_BYTES:
            return Response({"code": "file_too_large", "message": "That file is too large."}, status=413)
        try:
            uploads.receive_body(asset, request.body)
        except MediaError as error:
            return _refused(error)
        return Response({"asset": serializers.asset(asset)})


class CompleteView(APIView):
    """Confirm an upload is finished. Idempotent - safe to call again if a previous answer was lost."""

    def post(self, request, school_id, asset_id):
        membership = permissions.acting_membership(request, school_id)
        asset = permissions.get_asset(membership, asset_id)
        if permissions.uploaded_by_id(asset) != str(membership.id):
            raise PermissionDenied("Only the person who started this upload may complete it.")
        try:
            asset = uploads.complete(membership, asset)
        except MediaError as error:
            return _refused(error)
        return Response({"asset": serializers.asset(asset)})


class DownloadInfoView(APIView):
    """Where to read this file (or, with ?thumbnail=1, its preview) back from - always JSON, so the app never has
    to guess a response's shape from which storage backend happens to be configured: a short-lived presigned
    address for object storage (`mode: "redirect"`), or this app's own address to stream it from
    (`mode: "stream"`, `url` relative - fetch it the same authenticated way as everything else)."""

    def get(self, request, school_id, asset_id):
        membership = permissions.acting_membership(request, school_id)
        asset = permissions.get_asset(membership, asset_id)
        want_thumbnail = request.query_params.get("thumbnail") == "1"
        try:
            instructions, _storage_key, mime_type = uploads.open_download(membership, asset, thumbnail=want_thumbnail)
        except MediaError as error:
            return _refused(error)
        if instructions.mode == "stream":
            suffix = "?thumbnail=1" if want_thumbnail else ""
            instructions = replace(instructions, url=f"assets/{asset_id}/raw/{suffix}")
        return Response(serializers.download(instructions, mime_type))


class RawView(APIView):
    """The actual bytes, streamed - local storage only (see storage/local.py's module docstring). Reached only
    through the relative `url` DownloadInfoView hands back for `mode: "stream"`; checks the same authorisation
    again itself, so this address is never trusted just because a caller found it."""

    def get(self, request, school_id, asset_id):
        membership = permissions.acting_membership(request, school_id)
        asset = permissions.get_asset(membership, asset_id)
        want_thumbnail = request.query_params.get("thumbnail") == "1"
        try:
            instructions, storage_key, mime_type = uploads.open_download(membership, asset, thumbnail=want_thumbnail)
        except MediaError as error:
            return _refused(error)
        if instructions.mode != "stream":
            return Response({"code": "wrong_storage_mode", "message": "This server does not stream files directly."}, status=400)

        storage = get_storage()
        if not isinstance(storage, LocalMediaStorage):
            return Response({"code": "storage_error", "message": "This server cannot stream this file."}, status=500)
        path = storage.absolute_path(storage_key)
        if not path.is_file():
            raise Http404
        response = FileResponse(open(path, "rb"), content_type=mime_type)  # noqa: SIM115 - FileResponse closes it
        name = (asset.original_filename or asset.stored_filename).replace('"', "")
        response["Content-Disposition"] = f'inline; filename="{name}"'
        return response


class RetireView(APIView):
    def post(self, request, school_id, asset_id):
        membership = permissions.acting_membership(request, school_id)
        asset = permissions.get_asset(membership, asset_id)
        asset = uploads.retire(membership, asset, reason=request.data.get("reason", ""))
        return Response({"asset": serializers.asset(asset)})


class AuditView(APIView):
    def get(self, request, school_id, asset_id):
        membership = permissions.acting_membership(request, school_id)
        asset = permissions.get_asset(membership, asset_id)
        permissions.require_manage(membership, asset.owner_type, asset.owner_id, uploaded_by=permissions.uploaded_by_id(asset))
        events = MediaAuditEvent.objects.filter(school=membership.school, object_type="MediaAsset", object_id=str(asset.id))[:100]
        return Response({"events": [serializers.audit_event(e) for e in events]})
