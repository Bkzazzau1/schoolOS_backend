"""Who may upload, see, and retire a file - decided entirely by the owner kind the owning feature registered
(registry.py), never by a visibility string alone, and always re-checked against the acting membership's own
school, whatever school id or owner id a request names.
"""

from apps.media.constants import Status

from .base import MediaTestCase, make_png


class GalleryPermissionTests(MediaTestCase):
    """Gallery's own Spec: manage = proprietor/principal/administrator, contribute = teacher/staff,
    read = everyone unless the album is "internal" (staff only)."""

    def test_a_teacher_may_upload_to_an_album_and_a_parent_may_not(self):
        ok = self.initiate(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png(), who=self.teacher)
        self.assertEqual(ok.status_code, 201)
        refused = self.initiate(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png(), who=self.parent)
        self.assertEqual(refused.status_code, 403)

    def test_a_student_cannot_see_an_internal_staff_only_album(self):
        self.upload_all(owner_type="gallery_media_album", owner_id=self.internal_album_id, category="gallery_photo", data=make_png(), who=self.teacher)
        as_student = self.get(f"assets/?ownerType=gallery_media_album&ownerId={self.internal_album_id}", who=self.student)
        self.assertEqual(as_student.status_code, 403)
        as_principal = self.get(f"assets/?ownerType=gallery_media_album&ownerId={self.internal_album_id}", who=self.principal)
        self.assertEqual(as_principal.status_code, 200)

    def test_everyone_may_see_an_album_that_is_not_marked_internal(self):
        self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png(), who=self.teacher)
        for who in (self.parent, self.student, self.teacher, self.owner):
            response = self.get(f"assets/?ownerType=gallery_media_album&ownerId={self.album_id}", who=who)
            self.assertEqual(response.status_code, 200, who.role)

    def test_only_a_manager_or_whoever_uploaded_it_may_retire_a_photo(self):
        asset = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png(), who=self.teacher)
        # a different contributor role (staff) did not upload this one and does not manage the album
        self.assertEqual(self.members["staff"].role, "staff")
        refused = self.post(f"assets/{asset['id']}/retire/", who=self.members["staff"])
        self.assertEqual(refused.status_code, 403)
        by_uploader = self.post(f"assets/{asset['id']}/retire/", who=self.teacher)
        self.assertEqual(by_uploader.status_code, 200)

        asset2 = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png(), who=self.teacher)
        by_manager = self.post(f"assets/{asset2['id']}/retire/", who=self.principal)
        self.assertEqual(by_manager.status_code, 200)

    def test_uploading_a_video_that_is_not_actually_a_video_type_the_gallery_category_expects_is_refused(self):
        response = self.initiate(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_video", data=b"not really a video", mime_type="video/mp4", who=self.teacher)
        # accepted at initiate (mime type is allowed for the category); refused once the bytes are checked
        self.assertEqual(response.status_code, 201)
        asset_id = response.json()["asset"]["id"]
        put = self.put_body(f"assets/{asset_id}/upload/", b"not really a video", "video/mp4", who=self.teacher)
        self.assertEqual((put.status_code, put.json()["code"]), (400, "mime_type_mismatch"))


class StaffDocumentPermissionTests(MediaTestCase):
    def test_the_staff_member_themselves_may_upload_their_own_document(self):
        response = self.initiate(owner_type="staff_profile_document", owner_id=self.staff_id, category="staff_document", data=b"%PDF-1.4", mime_type="application/pdf", who=self.teacher)
        self.assertEqual(response.status_code, 201)

    def test_an_administrator_may_see_but_not_upload_a_staff_document(self):
        self.upload_all(owner_type="staff_profile_document", owner_id=self.staff_id, category="staff_document", data=b"%PDF-1.4", mime_type="application/pdf", who=self.teacher)
        can_see = self.get(f"assets/?ownerType=staff_profile_document&ownerId={self.staff_id}", who=self.administrator)
        self.assertEqual(can_see.status_code, 200)
        cannot_upload = self.initiate(owner_type="staff_profile_document", owner_id=self.staff_id, category="staff_document", data=b"%PDF-1.4", mime_type="application/pdf", who=self.administrator)
        self.assertEqual(cannot_upload.status_code, 403)

    def test_a_different_teacher_cannot_see_or_upload_someone_elses_document(self):
        other_teacher = self.members["driver"]  # any role that is not the linked staff member, owner or principal
        cannot_see = self.get(f"assets/?ownerType=staff_profile_document&ownerId={self.staff_id}", who=other_teacher)
        self.assertEqual(cannot_see.status_code, 403)
        cannot_upload = self.initiate(owner_type="staff_profile_document", owner_id=self.staff_id, category="staff_document", data=b"%PDF-1.4", mime_type="application/pdf", who=other_teacher)
        self.assertEqual(cannot_upload.status_code, 403)


class UnknownOwnerTests(MediaTestCase):
    def test_an_owner_type_nothing_has_registered_is_refused_as_not_found(self):
        response = self.initiate(owner_type="not_a_real_owner_type", owner_id="x", category="gallery_photo", data=make_png(), who=self.owner)
        self.assertEqual(response.status_code, 404)

    def test_an_owner_id_that_does_not_exist_is_refused_as_not_found_not_forbidden(self):
        response = self.initiate(owner_type="gallery_media_album", owner_id="no-such-album", category="gallery_photo", data=make_png(), who=self.teacher)
        self.assertEqual(response.status_code, 404)


class TenantIsolationTests(MediaTestCase):
    def test_naming_another_schools_album_from_this_school_is_not_found(self):
        response = self.initiate(owner_type="gallery_media_album", owner_id=self.other_school_album_id, category="gallery_photo", data=make_png(), who=self.owner)
        self.assertEqual(response.status_code, 404)

    def test_an_asset_id_from_another_school_is_not_found_here_even_for_that_schools_own_owner(self):
        asset = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png(), who=self.teacher)
        response = self.get(f"assets/{asset['id']}/", who=self.other_owner, school=self.other_school)
        self.assertEqual(response.status_code, 404)

    def test_a_download_url_for_an_asset_in_another_school_is_refused(self):
        asset = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png(), who=self.teacher)
        response = self.get(f"assets/{asset['id']}/download/", who=self.other_owner, school=self.other_school)
        self.assertEqual(response.status_code, 404)

    def test_retiring_an_asset_by_naming_the_wrong_school_in_the_url_does_not_touch_it(self):
        asset = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png(), who=self.teacher)
        response = self.post(f"assets/{asset['id']}/retire/", who=self.other_owner, school=self.other_school)
        self.assertEqual(response.status_code, 404)
        from apps.media.models import MediaAsset

        self.assertEqual(MediaAsset.objects.get(id=asset["id"]).status, Status.AVAILABLE)
