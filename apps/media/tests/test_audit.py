"""An append-only trail of what happened to a file, readable by whoever manages its owner, never a secret or the
file's own bytes."""

from apps.media.models import MediaAuditEvent

from .base import MediaTestCase, make_png


class AuditTests(MediaTestCase):
    def test_the_life_of_a_file_is_recorded_in_order(self):
        asset = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png(), who=self.teacher)
        self.get(f"assets/{asset['id']}/download/", who=self.owner)
        self.post(f"assets/{asset['id']}/retire/", {"reason": "test"}, who=self.teacher)
        events = self.get(f"assets/{asset['id']}/audit/", who=self.owner).json()["events"]
        kinds = [e["kind"] for e in reversed(events)]  # oldest first
        self.assertEqual(kinds, ["upload_initiated", "upload_completed", "upload_verified", "upload_available", "download_opened", "retired", "bytes_purged"])

    def test_nothing_in_the_audit_trail_ever_carries_a_secret_or_the_files_own_bytes(self):
        asset = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png(), who=self.teacher)
        for event in MediaAuditEvent.objects.filter(school=self.school):
            blob = str(event.detail)
            self.assertNotIn("PNG", blob)
            self.assertNotIn("checksum", blob.lower())

    def test_a_stored_audit_event_can_never_be_edited_or_deleted(self):
        asset = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png(), who=self.teacher)
        event = MediaAuditEvent.objects.filter(object_id=str(asset["id"])).first()
        event.kind = "tampered"
        with self.assertRaises(Exception):
            event.save()
        with self.assertRaises(Exception):
            event.delete()

    def test_only_someone_who_manages_the_owner_may_read_the_audit_trail(self):
        asset = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png(), who=self.teacher)
        refused = self.get(f"assets/{asset['id']}/audit/", who=self.parent)
        self.assertEqual(refused.status_code, 403)
        allowed = self.get(f"assets/{asset['id']}/audit/", who=self.teacher)  # uploaded it themselves
        self.assertEqual(allowed.status_code, 200)
