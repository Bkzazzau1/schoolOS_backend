import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.schools.models import Membership, School

from . import constants


class MediaAsset(models.Model):
    """One file SchoolOS knows about: a profile photo, a Gallery photo, a staff document, a generated receipt.

    The bytes themselves live in whatever `storage_provider` says (never in this row, and never in Postgres);
    this is the canonical metadata record every feature that shows or lists files reads from. `owner_type` and
    `owner_id` name the record this file belongs to (see registry.OwnerKind) without this app knowing what a
    Gallery album or a staff profile is. `visibility` is a hint for how a file is generally meant to be shown; it
    is never, on its own, what decides whether a particular person may see one - see permissions.py.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="media_assets")
    #: Who uploaded it. Kept even if that membership is later deactivated; SET_NULL rather than CASCADE because
    #: the file and its audit trail must outlive the person who added it.
    uploaded_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    owner_type = models.CharField(max_length=64)
    owner_id = models.CharField(max_length=128)
    #: What this file is for within its owner (e.g. "gallery_photo", "staff_document"). See constants.CATEGORIES.
    category = models.CharField(max_length=64)

    #: What the device called it. Shown to people; never trusted for anything else (not the stored path, not the
    #: assumed type - see validation.py).
    original_filename = models.CharField(max_length=255, blank=True)
    #: The name it is actually stored under: a random id plus a server-chosen extension. Never derived from
    #: original_filename.
    stored_filename = models.CharField(max_length=140)

    mime_type = models.CharField(max_length=120)
    media_type = models.CharField(max_length=20, choices=constants.MediaType.choices)
    byte_size = models.BigIntegerField()
    checksum_sha256 = models.CharField(max_length=64)

    storage_provider = models.CharField(max_length=10, choices=constants.StorageProvider.choices)
    #: The backend-specific path/object key. Random, never guessable from the filename or the asset id.
    storage_key = models.CharField(max_length=512, unique=True)

    visibility = models.CharField(max_length=10, choices=constants.Visibility.choices, default=constants.Visibility.PRIVATE)
    status = models.CharField(max_length=20, choices=constants.Status.choices, default=constants.Status.PENDING_UPLOAD)
    failure_code = models.CharField(max_length=40, blank=True)

    caption = models.CharField(max_length=500, blank=True)
    #: Set once a verify/thumbnail job has actually looked at an image; None for anything else or not yet looked at.
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    #: Bumped whenever the file itself is replaced (not on a metadata-only change like a caption edit), so a
    #: client caching a thumbnail or a download URL knows when to ask again.
    version = models.PositiveIntegerField(default=1)

    class Meta:
        indexes = [
            models.Index(fields=["school", "owner_type", "owner_id"]),
            models.Index(fields=["school", "status"]),
        ]
        constraints = [
            # A row is retired if and only if it carries a retirement time - never one without the other.
            models.CheckConstraint(
                check=(Q(status=constants.Status.RETIRED) & Q(deleted_at__isnull=False))
                | (~Q(status=constants.Status.RETIRED) & Q(deleted_at__isnull=True)),
                name="media_asset_retired_matches_deleted_at",
            ),
        ]

    def __str__(self):
        return f"{self.owner_type}/{self.owner_id}: {self.original_filename or self.stored_filename}"

    @property
    def is_image(self) -> bool:
        return self.media_type == constants.MediaType.IMAGE


class MediaDerivative(models.Model):
    """A file generated FROM an asset for cheap, repeated display - today only an image thumbnail, so a gallery
    grid or a profile photo never has to fetch the full-size original just to show a preview.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asset = models.ForeignKey(MediaAsset, on_delete=models.CASCADE, related_name="derivatives")
    kind = models.CharField(max_length=20, default="thumbnail")
    storage_provider = models.CharField(max_length=10, choices=constants.StorageProvider.choices)
    storage_key = models.CharField(max_length=512, unique=True)
    mime_type = models.CharField(max_length=120)
    byte_size = models.PositiveIntegerField()
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["asset", "kind"], name="one_derivative_of_each_kind_per_asset"),
        ]

    def __str__(self):
        return f"{self.kind} of {self.asset_id}"


class MediaJob(models.Model):
    """Background work on an asset - verifying an upload, building a thumbnail, purging a retired file's bytes -
    run outside any database transaction by `apps/media/jobs.py`, the same claim/lease/backoff shape
    `apps/mandates/models/debit.py: MandateProviderJob` already uses. The row recording that work is due is
    always written in the same transaction as the decision that requires it; the work itself never is.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    asset = models.ForeignKey(MediaAsset, on_delete=models.CASCADE, related_name="jobs")
    kind = models.CharField(max_length=12, choices=constants.JobKind.choices)
    status = models.CharField(max_length=10, choices=constants.JobStatus.choices, default=constants.JobStatus.QUEUED)
    #: Unique per (asset, kind, attempt-round) so a duplicate enqueue of the same work is a no-op, not a second job.
    idempotency_key = models.CharField(max_length=140, unique=True)
    run_after = models.DateTimeField()
    lease_until = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error_code = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["run_after", "created_at"]
        indexes = [models.Index(fields=["status", "run_after"])]

    def __str__(self):
        return f"{self.kind} {self.status} for {self.asset_id}"


class MediaAuditEvent(models.Model):
    """An append-only record of what happened to a file: initiated, uploaded, verified, replaced, retired, an
    important download, a webhook or signature failure. Never a secret, a presigned URL, or file contents.
    """

    id = models.BigAutoField(primary_key=True)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    actor = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=40)
    object_type = models.CharField(max_length=40)
    object_id = models.CharField(max_length=64)
    detail = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["school", "-at"]),
            models.Index(fields=["school", "object_type", "object_id"]),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("A media audit event is never edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A media audit event is never deleted.")

    def __str__(self):
        return f"{self.kind} {self.object_type}/{self.object_id}"
