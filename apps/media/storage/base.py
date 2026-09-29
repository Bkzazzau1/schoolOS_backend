"""The storage interface the rest of the media app talks to, so nothing outside this package knows or cares
whether a school's files sit on local disk (development, tests) or in S3-compatible object storage (production -
Wasabi or any other S3-compatible provider, never hard-coded to one). Adding a provider later means one new class
here; nothing else in the media app changes.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


class MediaStorageError(Exception):
    """Something the storage backend itself refused or could not do. `code` is a stable string; never carries a
    credential or a presigned URL's signature in its message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class UploadInstructions:
    """How a device should get its bytes into storage. `mode` is one of:
    - "direct": PUT the raw body to `upload_url`, one of SchoolOS's own authenticated endpoints (local storage
      only - there is no external endpoint to presign against on a developer's disk).
    - "presigned_put": PUT the raw body straight to `upload_url`, an object-store address good only for this one
      key, for a short time, with no SchoolOS credential of any kind embedded in it or needed to use it.
    """

    mode: str
    upload_url: str
    method: str = "PUT"
    headers: dict = field(default_factory=dict)
    expires_at: datetime | None = None


@dataclass(frozen=True)
class DownloadInstructions:
    """How a device should read a file back. `mode` is one of:
    - "stream": GET SchoolOS's own download endpoint again; it streams the bytes itself, after checking the
      requester may see this file. (Local storage; also anywhere an authenticated stream is preferred to a
      redirect.)
    - "redirect": open `url`, a short-lived presigned address at the object store good for this one file only.
      SchoolOS's own authorisation already ran before this was issued; the URL itself needs no further token.
    """

    mode: str
    url: str
    expires_at: datetime | None = None


@dataclass(frozen=True)
class StoredObject:
    byte_size: int
    etag: str = ""


class MediaStorage(ABC):
    #: One of constants.StorageProvider's values, so a MediaAsset can record which backend holds its bytes.
    provider: str

    @abstractmethod
    def initiate_upload(self, storage_key: str, *, mime_type: str, asset_id) -> UploadInstructions:
        """What to tell the device so it can put the file's bytes where this key expects them."""

    @abstractmethod
    def verify_existence(self, storage_key: str) -> StoredObject | None:
        """The object's real size (and, where the backend offers one, an integrity tag), or None if nothing is
        there yet. Never trusts what a device claims to have uploaded - this asks the storage backend itself."""

    @abstractmethod
    def read_bytes(self, storage_key: str, *, max_bytes: int) -> bytes:
        """The object's bytes, for hashing or building a thumbnail. Refuses (raises MediaStorageError) anything
        over max_bytes, so a background job never pulls an unbounded amount of data into memory."""

    @abstractmethod
    def write_bytes(self, storage_key: str, data: bytes, *, mime_type: str) -> None:
        """Store bytes SchoolOS itself produced (a thumbnail; local storage's own upload receiver). Never used for
        an original upload of unknown size from a device - that goes straight to the backend, see initiate_upload."""

    @abstractmethod
    def open_download(self, storage_key: str, *, filename: str, mime_type: str, ttl_seconds: int) -> DownloadInstructions:
        """How to read this file back, for a request already found allowed to see it."""

    @abstractmethod
    def delete(self, storage_key: str) -> None:
        """Forget the bytes at this key. Idempotent: deleting an already-gone key is not an error."""
