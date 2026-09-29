"""Deciding, before anything is trusted, what an upload really is - never what the device claims it is.

Three separate things must agree before a file is accepted: the category the caller asked to upload into (which
fixes a media type and a byte ceiling), the mime type it declares, and the actual bytes' own magic-byte signature.
The filename is never more than a label: what is stored on disk/in the bucket is always a fresh random name with
a server-chosen extension.
"""

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import date

from . import constants


class UploadRefused(Exception):
    """A file that fails validation. `code` is a stable string the API returns; `message` is for a person."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class UploadIntent:
    """What has been checked and decided about an upload before any bytes exist: the category is real, the media
    type and byte ceiling that implies, the mime type it may be stored as, and the exact key it will be stored
    under."""

    category: str
    media_type: str
    mime_type: str
    extension: str
    max_bytes: int
    storage_key: str
    stored_filename: str
    safe_original_filename: str


_FILENAME_KEEP = re.compile(r"[^A-Za-z0-9 ._-]+")
_WHITESPACE = re.compile(r"\s+")


def sanitize_filename(name: str) -> str:
    """A filename fit to show a person: no path, no control characters, no run of the device's own choosing that
    could collide with or overwrite something else - because it is a label only and is never used to build a
    stored path.
    """
    name = (name or "").strip()
    # Neither a Linux nor a Windows path separator survives, whichever server or device sent it.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _FILENAME_KEEP.sub(" ", name)
    name = _WHITESPACE.sub(" ", name).strip(" .")
    return name[:150] or "file"


def check_intent(*, category: str, declared_mime_type: str, declared_byte_size: int, original_filename: str, school_id) -> UploadIntent:
    """Everything that can be decided before a single byte has arrived: is this a category SchoolOS knows about,
    is the declared mime type one that category accepts, and is the declared size within its ceiling. Raises
    `UploadRefused` (never silently narrows or guesses) for anything that does not check out.
    """
    media_type = constants.media_type_for_category(category)
    if media_type is None:
        raise UploadRefused("unknown_category", "SchoolOS does not know what this kind of file is for.")

    allowed = constants.ALLOWED_MIME_TYPES.get(media_type, {})
    extension = allowed.get(declared_mime_type)
    if extension is None:
        raise UploadRefused("mime_type_not_allowed", "That kind of file is not accepted here.")

    if not isinstance(declared_byte_size, int) or declared_byte_size <= 0:
        raise UploadRefused("invalid_size", "The file's size was not given properly.")
    max_bytes = constants.max_bytes_for(category, media_type)
    if declared_byte_size > max_bytes:
        raise UploadRefused("file_too_large", f"That file is larger than the {max_bytes // (1024 * 1024)} MB this allows.")

    stored_filename = f"{uuid.uuid4().hex}{extension}"
    today = date.today()
    # The school id is folded into the key so nothing about the storage layout has to be trusted to keep one
    # school's files out of another's path space, even though authorisation is decided in the database, never
    # by the key being hard to guess.
    storage_key = f"{school_id}/{media_type}/{today.year:04d}/{today.month:02d}/{stored_filename}"

    return UploadIntent(
        category=category,
        media_type=media_type,
        mime_type=declared_mime_type,
        extension=extension,
        max_bytes=max_bytes,
        storage_key=storage_key,
        stored_filename=stored_filename,
        safe_original_filename=sanitize_filename(original_filename),
    )


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


#: (media type, signature-checking function). Checked in order; the first that matches wins. A media type with no
#: entry here (audio, generic documents beyond what is listed) is trusted on its declared mime type alone once it
#: has passed the category/size checks above - real byte-level sniffing for those is a future improvement, not a
#: security promise this module makes today.
def _is_png(head: bytes) -> bool:
    return head.startswith(b"\x89PNG\r\n\x1a\n")


def _is_jpeg(head: bytes) -> bool:
    return head.startswith(b"\xff\xd8\xff")


def _is_gif(head: bytes) -> bool:
    return head.startswith(b"GIF87a") or head.startswith(b"GIF89a")


def _is_webp(head: bytes) -> bool:
    return head.startswith(b"RIFF") and head[8:12] == b"WEBP"


def _is_pdf(head: bytes) -> bool:
    return head.startswith(b"%PDF-")


def _is_mp4_family(head: bytes) -> bool:
    # An ISO base media file (mp4, mov, m4a, ...) names its brand at byte offset 4: "....ftyp....".
    return head[4:8] == b"ftyp"


def _is_webm(head: bytes) -> bool:
    return head.startswith(b"\x1a\x45\xdf\xa3")


def _is_ogg(head: bytes) -> bool:
    return head.startswith(b"OggS")


def _is_mp3(head: bytes) -> bool:
    return head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")


def _is_wav(head: bytes) -> bool:
    return head.startswith(b"RIFF") and head[8:12] == b"WAVE"


def _is_office_zip(head: bytes) -> bool:
    # docx/xlsx/pptx are all zip containers; the mime type on the intent (already checked against the category)
    # says which one was declared, so only the zip signature itself needs confirming here.
    return head.startswith(b"PK\x03\x04")


_SNIFFERS: dict[str, callable] = {
    "image/png": _is_png,
    "image/jpeg": _is_jpeg,
    "image/gif": _is_gif,
    "image/webp": _is_webp,
    "application/pdf": _is_pdf,
    "video/mp4": _is_mp4_family,
    "video/quicktime": _is_mp4_family,
    "audio/mp4": _is_mp4_family,
    "video/webm": _is_webm,
    "audio/ogg": _is_ogg,
    "audio/mpeg": _is_mp3,
    "audio/wav": _is_wav,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": _is_office_zip,
}


def check_signature(*, declared_mime_type: str, head: bytes) -> None:
    """Refuse a file whose first bytes do not match what it claims to be - a renamed executable, an image that is
    really something else. A mime type with no sniffer here (plain text, legacy .doc, formats too varied to sniff
    cheaply) is not checked at this level; the category/mime/size checks in `check_intent` still applied.
    """
    sniffer = _SNIFFERS.get(declared_mime_type)
    if sniffer is not None and not sniffer(head):
        raise UploadRefused("mime_type_mismatch", "This file's contents do not match the kind of file it claims to be.")
