"""Local-disk storage: development and tests only, never production (see the module docstring on `base.py`).

There is no external address to hand a device a presigned URL for, so a local upload instead PUTs its bytes to
SchoolOS's own authenticated endpoint (`views.UploadBodyView`), which calls `write_bytes` here. A download is
streamed the same way, through SchoolOS's own authenticated endpoint - never a bare filesystem path handed to a
client.
"""

from pathlib import Path

from .base import DownloadInstructions, MediaStorage, MediaStorageError, StoredObject, UploadInstructions


class LocalMediaStorage(MediaStorage):
    provider = "local"

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def _path(self, storage_key: str) -> Path:
        path = (self.root / storage_key).resolve()
        # A storage key is always server-generated (see validation.check_intent) and never taken from a device
        # as-is, but this still refuses to ever touch a path outside the media root, belt and braces.
        if path != self.root and self.root not in path.parents:
            raise MediaStorageError("invalid_key", "That storage key is not valid.")
        return path

    def initiate_upload(self, storage_key, *, mime_type, asset_id):
        return UploadInstructions(mode="direct", upload_url=f"assets/{asset_id}/upload/", method="PUT")

    def verify_existence(self, storage_key):
        path = self._path(storage_key)
        if not path.is_file():
            return None
        return StoredObject(byte_size=path.stat().st_size)

    def read_bytes(self, storage_key, *, max_bytes):
        path = self._path(storage_key)
        if not path.is_file():
            raise MediaStorageError("not_found", "That file is not in storage.")
        if path.stat().st_size > max_bytes:
            raise MediaStorageError("too_large_to_read", "That file is too large to process here.")
        return path.read_bytes()

    def write_bytes(self, storage_key, data, *, mime_type):
        path = self._path(storage_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def open_download(self, storage_key, *, filename, mime_type, ttl_seconds):
        return DownloadInstructions(mode="stream", url="")

    def delete(self, storage_key):
        path = self._path(storage_key)
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            # On Windows a file still open for reading (a download in flight) cannot be unlinked yet. This is
            # transient, not a real failure: the purge job's own retry/backoff (see jobs.py) tries again shortly.
            raise MediaStorageError("file_in_use", "That file is still being read; it will be purged shortly.") from error

    # -- local-only: the streaming download view needs a real path; no other caller should reach for this ------

    def absolute_path(self, storage_key: str) -> Path:
        return self._path(storage_key)
