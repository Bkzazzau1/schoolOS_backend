# SchoolOS Media & Files (backend)

One canonical file/attachment service (`apps/media`), reused by every module that needs a real file, instead of a
new upload system per feature. This file says what is real, what is pending, and how the pieces fit.

```
A feature registers an OwnerKind for its own record            apps/schoollife/apps.py (Gallery, Community)
   (who may upload/see/manage a file attached to it)            apps/administration/apps.py (document records)
                                                                  apps/staff/apps.py (staff documents)
        |
        v
POST .../media/assets/  (initiate)  ->  a place is made for the file; local storage: an address on this server;
                                         S3-compatible storage: a short-lived presigned PUT straight to the bucket
        |
        v
The device puts the bytes there (never through Django for S3; a raw PUT to this app for local/dev)
        |
        v
POST .../assets/<id>/complete/  ->  MediaJob(verify) queued in the same transaction, run outside any transaction
        |                            (checksum, signature, image dimensions)
        v
verified  ->  (image) MediaJob(thumbnail)  ->  available          (anything else)  ->  available directly
        |
        v
GET .../assets/<id>/download/[?thumbnail=1]  ->  a short-lived presigned GET (S3) or an authenticated stream (local)
```

## Not a rewrite of anything that already works

Gallery's album, Community's post, a staff member's onboarding-document list, and an Administrator document
record are unchanged: each stays the generic `SyncRecord` it already was, with its own `Spec`/`EntityHandler`
still deciding who may write and see it (`apps/schoollife/framework.py`, `apps/sync/registry.py`). `apps/media`
never duplicates those rules - it asks the owning feature's already-registered handler the same two questions
the sync layer already answers (`apps/media/bridges/schoollife.py`), or, where a module is not built on that
shared engine (staff's own onboarding documents), mirrors its handler's rules exactly rather than writing a
different version of them (`apps/staff/media_owner.py`). Uploading a file never changes a document's own status
(`requested`/`received`/`verified`, `apps/staff/profiles/sections.py`) - a manager still decides that.

## The canonical model (`apps/media/models.py`)

`MediaAsset`: school, uploader, `(ownerType, ownerId)`, category, original filename (a label only), a random
stored filename and storage key, mime type, media type, byte size, sha-256 checksum, storage provider, storage
key, visibility, status, an optional caption, image width/height once known, timestamps, `deletedAt`, a version
counter. `MediaDerivative`: today only a `thumbnail` per asset. `MediaJob`: durable background work (verify,
thumbnail, purge), the same claim/lease/backoff shape as `apps/mandates/models/debit.py: MandateProviderJob`.
`MediaAuditEvent`: append-only (enforced in code, like `apps/mandates/models/debit.py: MandateAuditEvent`), never
carries a secret, a presigned URL, or the file's own bytes.

**A visibility string is never, on its own, what decides whether someone may see a file.** Every read, write and
manage action is checked against the owner's own rules (`apps/media/permissions.py` -> `registry.OwnerKind`).

## Owner kinds registered today

| `ownerType` | Registered by | Who may upload | Who may see | Who may retire |
|---|---|---|---|---|
| `gallery_media_album` | `apps/schoollife/apps.py` | teacher, staff, or a manager (proprietor/principal/administrator) | everyone, unless the album is marked `internal` (staff only) | a manager, or whoever uploaded that one file |
| `community_post` | `apps/schoollife/apps.py` | any adult member (the same WRITERS role set posting already uses) | as above | a moderator, or whoever uploaded that one file |
| `school_excursion` | `apps/schoollife/apps.py` | a manager, or the teacher who added that trip | everyone the excursion module already reaches | a manager, or whoever uploaded that one file |
| `administrator_document_record` | `apps/administration/apps.py` | the administrator | proprietor, principal, administrator | the administrator |
| `staff_profile_document` | `apps/staff/apps.py` | the owner, the principal, or the staff member themselves (once their account is linked) | the above, plus the administrator (view only - documents are part of the file the administrator already reviews) | the owner, the principal, or the staff member themselves |
| `principal_recorded_incident_case` | `apps/administration/apps.py` | the principal only | the principal only - not even the owner reads this | the principal only |
| `school_appearance` | `apps/structure/apps.py` | the proprietor only | everyone in the school | the proprietor only |

Adding a new owner (a future messaging thread, say) is usually one `OwnerKind` registration in that feature's own
`AppConfig.ready()` against an entity type that already has a sync handler - nothing in `apps/media` changes. If
no sync entity exists yet either (as was true for incident cases before this table's own `principal_recorded_
incident_case` row), that entity has to be built first - registering the media owner kind alone is not enough.
`apps/media/constants.py` already defines every category the app currently uses (`student_document`,
`admission_document`, `staff_document`, `community_attachment`, `excursion_evidence`, `incident_evidence`,
`school_logo`, `message_attachment`, `receipt`, `generated_report`, ...) plus `gallery_photo`/`gallery_video`; a
category is usable under any owner as soon as that owner exists.

## Storage (`apps/media/storage/`)

`MediaStorage` is the interface (`base.py`); `LocalMediaStorage` (development and tests only - writes under
`MEDIA_LOCAL_ROOT`, streamed back through this server's own authenticated endpoint) and `S3MediaStorage`
(production - any S3-compatible provider, Wasabi included, via `MEDIA_S3_ENDPOINT_URL`; never a host hard-coded to
one provider) are its two implementations. `get_storage()` picks one from `MEDIA_STORAGE_BACKEND`; `use_storage()`
(a context manager, the same shape as `apps/bankconnect/providers/transport.py: use_transport`) lets a test swap
in a fake backend, proving the upload API works unchanged against something that is not local disk.

**No storage credential is ever returned to a client.** Object-storage uploads and downloads are short-lived
presigned URLs, each good for one key and one operation; local storage never hands out a path, only a stream from
an endpoint that has already checked the requester may see the file.

## Validation (`apps/media/validation.py`)

A category (`apps/media/constants.CATEGORIES`) fixes a media type, which fixes the mime types accepted and a byte
ceiling; a category may set a smaller ceiling of its own, never a larger one. The declared mime type must match
the category; the bytes' own magic-byte signature must match the declared mime type for every image and document
type SchoolOS knows how to sniff (PNG, JPEG, GIF, WEBP, PDF, the ISO-base-media family covering MP4/MOV/M4A,
WEBM, OGG, MP3, WAV, and the zip signature docx/xlsx/pptx share) - a renamed file is refused, not trusted. The
filename is a label only: what is stored is always a fresh random id plus a server-chosen extension, never
anything derived from what the device called it or from its own claimed extension.

## Lifecycle and background jobs (`apps/media/jobs.py`, `apps/media/uploads.py`)

`pending_upload -> uploaded -> verified -> available`, with `failed` for a checksum/signature/size mismatch and
`retired` for a soft delete. `quarantined` is real now too: verifying a file also scans it for malware
(`apps/media/scanning.py`) - an infected file is quarantined instead of verified, so it is never thumbnailed and
never downloadable (`open_download`'s own allow-list already only accepts `available`/`verified`; quarantine
needed no new filtering there), the same way a checksum mismatch already stops a file short of `verified` today.
Verifying (re-reading the real bytes, hashing them, checking the signature again, scanning for malware, probing
an image's dimensions) and building a thumbnail run from a durable job queue outside any database transaction -
`manage.py run_media_jobs [--loop]`, or, where no worker runs, a bounded inline drain right after the request
that queued the work. A transient storage error retries with backoff; a checksum mismatch, an invalid image or a
detected infection is a permanent outcome, recorded once, never retried forever. Completing an upload a second
time (a lost response, a retried request) is a safe no-op - this is also how a client reconciles an upload it
lost track of.

## Thumbnails (`apps/media/thumbnails.py`, `apps/media/transcoding.py`)

An image always gets a 480px-max-side JPEG thumbnail, orientation corrected first (a photo taken sideways is
never stored rotated) - Pillow, unchanged.

A video's own preview frame goes through `transcoding.py`'s `VideoTranscoder` seam instead - the same
override-for-tests shape `apps/media/storage`'s `use_storage`/`get_storage` already uses for storage backends.
The real implementation, `FfmpegTranscoder`, shells out to a real `ffmpeg` binary (`shutil.which("ffmpeg")`) to
grab the first frame and shrink it to the same target size the image path uses. **Whether a video actually gets a
thumbnail depends on whether this server has `ffmpeg` installed** - where it is not (this repository's own
development/test environment has none), the video is still verified, stored, downloadable and marked `available`
exactly as before; it simply has no thumbnail, the same honest gap as always, now reached through a real job
attempt rather than never being tried at all. Nothing about this is a job failure or a retry: `TranscoderUnavailable`
is a known, expected condition `run_thumbnail` treats the same way a permanent decode failure is - the asset
becomes available without a preview, never stuck retrying, never marked failed over something nobody did wrong.
Install a real `ffmpeg` binary on a server and video thumbnails start working with no further code change.

## Malware scanning (`apps/media/scanning.py`)

Every upload is scanned as part of verifying it, through the same override-for-tests seam shape as storage and
video transcoding: a `MalwareScanner` interface, a real `ClamAvScanner` implementation that shells out to a real
`clamscan` binary (`shutil.which("clamscan")`), and `use_scanner`/`get_scanner` module-level swap functions for
tests. **Whether an upload is actually scanned depends on whether this server has ClamAV's `clamscan` installed**
- where it is not (this repository's own development/test environment has none), the upload still verifies
normally; it simply was not checked for malware, the same honest gap the `quarantined` status has always
documented. This is never a job failure or a retry: `ScannerUnavailable` (and, symmetrically, a genuine scan
failure that is not the uploader's fault either) is caught the same way `TranscoderUnavailable` already is - the
upload proceeds to `verified` as normal. An actual detection is different: the file is quarantined instead of
verified, so it is never thumbnailed and never downloadable, and the quarantine is recorded in the audit trail.
Install a real `clamscan` binary on a server and scanning starts working with no further code change.

## Deletion

Retiring an asset is a soft delete: `status=retired`, `deletedAt` set, the row and its whole audit trail kept.
Its bytes are purged by their own background job, not inline, so a purge failure (on Windows, a file the last
download has not finished streaming yet) retries rather than blocking the retire itself.

## Settings

`MEDIA_STORAGE_BACKEND` (`local` default, `s3` for production), `MEDIA_LOCAL_ROOT`, `MEDIA_S3_BUCKET`,
`MEDIA_S3_REGION`, `MEDIA_S3_ENDPOINT_URL`, `MEDIA_S3_ACCESS_KEY`, `MEDIA_S3_SECRET_KEY`,
`MEDIA_DOWNLOAD_URL_TTL_SECONDS` (default 300), `MEDIA_INLINE_JOBS` / `MEDIA_INLINE_SECONDS` (the same shape as
`MANDATES_INLINE_JOBS`). New dependencies: `Pillow` (thumbnails, image dimensions), `boto3` (the S3-compatible
adapter) - neither existed in this repository before.

## API

`/api/v1/schools/<school>/media/assets/` (GET lists one owner's files with `?ownerType=&ownerId=`, POST initiates
an upload), `assets/<id>/` (metadata), `assets/<id>/upload/` (PUT the raw bytes - local storage only),
`assets/<id>/complete/`, `assets/<id>/download/` (`?thumbnail=1` for the preview), `assets/<id>/retire/`,
`assets/<id>/audit/`. Every call is scoped to the acting membership's own school first; an asset id or an owner id
from another school is a 404, identical to one that does not exist at all.

## Pending (nothing invented)

* **Video thumbnails / transcoding** - the real machinery (job dispatch, the `VideoTranscoder` seam,
  `MediaDerivative` storage, graceful degradation) is built and tested against a fake transcoder; what is
  actually pending is a real `ffmpeg` binary on a server. No environment this app has been built or tested in so
  far has one installed, so no video has ever actually received a real thumbnail yet - video uploads, storage
  and download work today regardless.
* **Malware/antivirus scanning** - likewise: the real machinery (the `MalwareScanner` seam, quarantining on
  detection, graceful degradation) is built and tested against a fake scanner; what is actually pending is a real
  `clamscan` binary on a server, which no environment this app has been built or tested in so far has - every
  upload verifies normally today, unscanned.
* **Messaging attachments** - registered ready for the day a Messaging feature exists to carry them; nothing
  calls the media API for them yet, since Messaging itself is not built.
* **Community post attachments, excursion evidence, incident evidence and the school logo** are wired into a real
  screen on the app side now - see the Flutter app's own `docs/BACKEND_INTEGRATION.md` for exactly what each
  looks like there. Incident evidence's own owner kind (`principal_recorded_incident_case`) needed a real new
  sync entity built for it (`apps/administration/specs.py`), not just a registration - it had none before.
