"""Gallery is the first real consumer: an album (its own existing SyncRecord, unchanged) can now carry real
photos and videos - captions, an uploader, a created date, several files - instead of only the "how many photos"
count it had before. Administrator's document records and a staff member's onboarding documents can now carry a
real file the same way, without either record's own workflow changing.
"""

from apps.media.constants import Status

from .base import MediaTestCase, make_png


class GalleryIntegrationTests(MediaTestCase):
    def test_an_album_can_hold_several_photos_each_with_its_own_caption_and_uploader(self):
        first = self.upload_all(
            owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo",
            data=make_png(color=(10, 10, 200)), who=self.teacher, caption="Opening the sports day",
        )
        second = self.upload_all(
            owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo",
            data=make_png(color=(200, 10, 10)), who=self.members["staff"], caption="The 100m final",
        )
        listed = self.get(f"assets/?ownerType=gallery_media_album&ownerId={self.album_id}", who=self.owner).json()["assets"]
        self.assertEqual({a["id"] for a in listed}, {first["id"], second["id"]})
        by_id = {a["id"]: a for a in listed}
        self.assertEqual(by_id[first["id"]]["caption"], "Opening the sports day")
        self.assertEqual(by_id[first["id"]]["uploadedByMembershipId"], str(self.teacher.id))
        self.assertEqual(by_id[second["id"]]["caption"], "The 100m final")
        self.assertTrue(all(a["status"] == Status.AVAILABLE and a["hasThumbnail"] for a in listed))

    def test_a_video_is_accepted_stored_and_downloadable_but_gets_no_thumbnail_yet(self):
        # Video thumbnailing is not built in this phase (see docs/MEDIA.md); the file itself is still real,
        # stored and servable - never faked as "not supported at all".
        fake_mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
        asset = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_video", data=fake_mp4, mime_type="video/mp4", file_name="sports-day.mp4", who=self.teacher)
        self.assertEqual(asset["status"], Status.AVAILABLE)
        self.assertFalse(asset["hasThumbnail"])
        downloaded = self.get(f"assets/{asset['id']}/download/", who=self.owner)
        self.assertEqual(downloaded.status_code, 200)

    def test_removing_a_photo_from_an_album_is_a_retire_not_a_hard_delete(self):
        asset = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png(), who=self.teacher)
        self.post(f"assets/{asset['id']}/retire/", {"reason": "Blurry"}, who=self.principal)
        from apps.media.models import MediaAsset

        self.assertTrue(MediaAsset.objects.filter(id=asset["id"]).exists())  # the row survives
        self.assertEqual(MediaAsset.objects.get(id=asset["id"]).status, Status.RETIRED)


class AdministratorDocumentRecordIntegrationTests(MediaTestCase):
    """The generic `administrator_document_record` (its own "document" field stays a label; a real file can now
    sit alongside it) - Administrator manages it, Owner/Principal read it, nobody else does, exactly as
    `apps/administration/specs.py: DOCUMENT_RECORDS` already says."""

    def test_the_administrator_can_attach_and_see_a_real_file_on_a_document_record(self):
        asset = self.upload_all(owner_type="administrator_document_record", owner_id=self.document_record_id, category="admission_document", data=b"%PDF-1.4", mime_type="application/pdf", who=self.administrator)
        self.assertEqual(asset["status"], Status.AVAILABLE)
        seen_by_owner = self.get(f"assets/?ownerType=administrator_document_record&ownerId={self.document_record_id}", who=self.owner)
        self.assertEqual(seen_by_owner.status_code, 200)
        self.assertEqual(len(seen_by_owner.json()["assets"]), 1)

    def test_a_teacher_cannot_see_or_attach_a_document_record_file(self):
        cannot_upload = self.initiate(owner_type="administrator_document_record", owner_id=self.document_record_id, category="admission_document", data=b"%PDF-1.4", mime_type="application/pdf", who=self.teacher)
        self.assertEqual(cannot_upload.status_code, 403)
        cannot_view = self.get(f"assets/?ownerType=administrator_document_record&ownerId={self.document_record_id}", who=self.teacher)
        self.assertEqual(cannot_view.status_code, 403)


class StaffDocumentIntegrationTests(MediaTestCase):
    def test_uploading_a_document_does_not_by_itself_change_the_profiles_own_document_status(self):
        """Uploading a file is not the same as the record being marked received or verified - that stays a
        person's own decision on the profile record, unaffected by anything the media app does."""
        from apps.sync.models import SyncRecord
        from apps.staff.constants import PROFILE as STAFF_PROFILE

        record = SyncRecord.objects.get(school=self.school, entity_type=STAFF_PROFILE, entity_id=self.staff_id)
        record.payload = {**record.payload, "documents": [{"name": "Curriculum vitae", "status": "requested", "reference": ""}]}
        record.save()

        self.upload_all(owner_type="staff_profile_document", owner_id=self.staff_id, category="staff_document", data=b"%PDF-1.4", mime_type="application/pdf", who=self.teacher)

        record.refresh_from_db()
        self.assertEqual(record.payload["documents"][0]["status"], "requested")  # unchanged: a manager still decides this
