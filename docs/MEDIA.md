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
| `community_post` | `apps/schoollife/apps.py` | registered ready for the day Community's own `mediaLabel` caption is replaced by a real attachment - not wired into any screen yet | as above | as above |
| `administrator_document_record` | `apps/administration/apps.py` | the administrator | proprietor, principal, administrator | the administrator |
| `staff_profile_document` | `apps/staff/apps.py` | the owner, the principal, or the staff member themselves (once their account is linked) | the above, plus the administrator (view only - documents are part of the file the administrator already reviews) | the owner, the principal, or the staff member themselves |

Adding a new owner (excursion evidence, an incident report, a future messaging thread) is one `OwnerKind`
registration in that feature's own `AppConfig.ready()` - nothing in `apps/media` changes. `apps/media/constants.py`
already defines the categories the brief asked for (`student_document`, `admission_document`, `excursion_evidence`,
`incident_evidence`, `message_attachment`, `receipt`, `generated_report`, ...); a category is usable under any
owner as soon as that owner exists, so student and admission documents can already be attached to
`administrator_document_record` today, category `student_document` / `admission_document`.

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
`retired` for a soft delete (`quarantined` exists for future malware scanning; nothing sets it yet). Verifying
(re-reading the real bytes, hashing them, checking the signature again, probing an image's dimensions) and
building a thumbnail run from a durable job queue outside any database transaction - `manage.py run_media_jobs
[--loop]`, or, where no worker runs, a bounded inline drain right after the request that queued the work. A
transient storage error retries with backoff; a checksum mismatch or an invalid image is a permanent failure,
recorded once, never retried forever. Completing an upload a second time (a lost response, a retried request) is
a safe no-op - this is also how a client reconciles an upload it lost track of.

## Thumbnails (`apps/media/thumbnails.py`)

Images only, today: a 480px-max-side JPEG, orientation corrected first (a photo taken sideways is never stored
rotated). **Video is validated, stored and served, but not yet transcoded or thumbnailed** - Pillow (already used
for images) has no video support, and adding a transcoding dependency was out of scope for this phase; the
architecture (a `MediaDerivative` per asset, one job kind) is ready for it without a rewrite.

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

* **Video thumbnails / transcoding** - not built this phase; video uploads, storage and download work today.
* **Malware/antivirus scanning** - the `quarantined` status and the job-queue shape exist for it; nothing scans
  today.
* **Community, excursion evidence, incident evidence, messaging attachments** - their owner kinds are either
  registered (`community_post`) or trivial to add the same one-registration way; no screen calls the media API
  for any of them yet - see the Flutter app's own docs for exactly what is wired into a screen today (Gallery)
  versus only backend-ready.
* **The school logo** (`lib/core/appearance` on the app side) still uses its own small base64-in-sync-payload
  path, capped at 140 KB, unchanged by this work; `school_logo` is a defined category, ready for that flow to
  move onto the real media service later without inventing a second one meanwhile.
