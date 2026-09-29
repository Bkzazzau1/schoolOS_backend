"""What a file is, who may see it, and where it stands in its own lifecycle - shared by every module that stores a
real file through SchoolOS (student and staff documents, admission documents, profile photos, the school's own
logo, Gallery photos and videos, Community attachments, excursion and incident evidence, generated receipts and
reports, and messaging attachments once that exists). One canonical file model, reused everywhere, rather than a
new upload system per feature.
"""

from django.db import models


class MediaType(models.TextChoices):
    IMAGE = "image", "Image"
    VIDEO = "video", "Video"
    AUDIO = "audio", "Audio"
    DOCUMENT = "document", "Document"
    #: A PDF or spreadsheet SchoolOS itself generated (a receipt, a report), not something a person uploaded.
    GENERATED_DOCUMENT = "generated_document", "Generated document"
    OTHER = "other", "Other"


class Visibility(models.TextChoices):
    #: Only the uploader, whoever manages the owning record, and anyone that record's own rules name.
    PRIVATE = "private", "Private"
    STAFF = "staff", "Staff"
    SCHOOL = "school", "Everyone at the school"
    PARENTS = "parents", "Parents"
    PUBLIC = "public", "Public"


#: Where a file stands. A visibility string is never enough on its own to decide who may read a file - see
#: apps/media/permissions.py - but the status decides whether it is offered at all.
class Status(models.TextChoices):
    #: A place has been made for it; the bytes have not arrived (or have not been confirmed) yet.
    PENDING_UPLOAD = "pending_upload", "Waiting for the upload"
    #: The bytes are in storage, but not yet checked against what the uploader declared.
    UPLOADED = "uploaded", "Uploaded"
    #: The stored bytes have been checked (size, checksum, that they really exist) and are trustworthy.
    VERIFIED = "verified", "Verified"
    #: Verified, and any derivative (a thumbnail) that should exist has been made. Safe to offer to a viewer.
    AVAILABLE = "available", "Available"
    #: The upload or its verification failed (a mismatch, a rejected type, a storage error).
    FAILED = "failed", "Failed"
    #: Held back from view pending a check SchoolOS cannot yet perform itself (reserved for future malware
    #: scanning; nothing sets this today, but the state exists so scanning can be added without a new status).
    QUARANTINED = "quarantined", "Quarantined"
    #: Soft-deleted. The row and its audit trail stay; the bytes may be reclaimed later under a retention policy.
    RETIRED = "retired", "Retired"


class StorageProvider(models.TextChoices):
    LOCAL = "local", "Local disk (development and tests)"
    S3 = "s3", "S3-compatible object storage"


class JobKind(models.TextChoices):
    #: Confirm the uploaded bytes really exist, are the declared size, and match the declared checksum.
    VERIFY = "verify", "Verify the upload"
    #: Build a thumbnail/preview derivative (images today; video is prepared for, not yet built - see thumbnails.py).
    THUMBNAIL = "thumbnail", "Build a thumbnail"
    #: Ask the storage backend to forget the bytes of a retired asset, once its retention period has passed.
    PURGE = "purge", "Purge retired bytes"


class JobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    RETRY = "retry", "Waiting to retry"
    SUCCEEDED = "succeeded", "Done"
    FAILED = "failed", "Failed"


MAX_ATTEMPTS = 6
BACKOFF_SECONDS = (30, 120, 600, 1800, 3600, 7200)
LEASE_SECONDS = 180

#: The most a category in this media type may weigh. A category (see CATEGORY_LIMITS) may set a smaller limit;
#: none may exceed its media type's ceiling here.
MAX_BYTES_BY_MEDIA_TYPE = {
    MediaType.IMAGE: 15 * 1024 * 1024,
    MediaType.VIDEO: 200 * 1024 * 1024,
    MediaType.AUDIO: 30 * 1024 * 1024,
    MediaType.DOCUMENT: 25 * 1024 * 1024,
    MediaType.GENERATED_DOCUMENT: 25 * 1024 * 1024,
    MediaType.OTHER: 10 * 1024 * 1024,
}

#: category -> a smaller byte ceiling than its media type's default, where one file of that kind should never be huge.
CATEGORY_MAX_BYTES = {
    "profile_photo": 6 * 1024 * 1024,
    "school_logo": 2 * 1024 * 1024,
}

#: The mime types a media type accepts, each mapped to the file extension SchoolOS stores it under (never the
#: extension the device sent - see validation.py). A mime type not listed here is refused outright.
ALLOWED_MIME_TYPES = {
    MediaType.IMAGE: {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    },
    MediaType.VIDEO: {
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
    },
    MediaType.AUDIO: {
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/wav": ".wav",
        "audio/ogg": ".ogg",
    },
    MediaType.DOCUMENT: {
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "text/plain": ".txt",
    },
    MediaType.GENERATED_DOCUMENT: {
        "application/pdf": ".pdf",
    },
    #: "other" accepts nothing by default: a category must be given a real media type before anything uploads
    #: under it. This is the deny-by-default the brief asks for, not an oversight.
    MediaType.OTHER: {},
}

#: category -> the media type it is stored and validated as. A category not listed here is refused: every upload
#: must declare a category a feature has actually registered a meaning for.
CATEGORIES = {
    # People
    "profile_photo": MediaType.IMAGE,
    "school_logo": MediaType.IMAGE,
    # Admissions, staff and student records
    "admission_document": MediaType.DOCUMENT,
    "student_document": MediaType.DOCUMENT,
    "staff_document": MediaType.DOCUMENT,
    # Gallery
    "gallery_photo": MediaType.IMAGE,
    "gallery_video": MediaType.VIDEO,
    # Community
    "community_attachment": MediaType.IMAGE,
    # School life evidence
    "excursion_evidence": MediaType.IMAGE,
    "incident_evidence": MediaType.IMAGE,
    # SchoolOS-generated
    "receipt": MediaType.GENERATED_DOCUMENT,
    "generated_report": MediaType.GENERATED_DOCUMENT,
    # Messaging (registered ahead of the feature that will use it, per the brief's "ready for... Messaging")
    "message_attachment": MediaType.IMAGE,
}


def media_type_for_category(category: str) -> str | None:
    return CATEGORIES.get(category)


def max_bytes_for(category: str, media_type: str) -> int:
    return min(CATEGORY_MAX_BYTES.get(category, 10**12), MAX_BYTES_BY_MEDIA_TYPE.get(media_type, 0))
