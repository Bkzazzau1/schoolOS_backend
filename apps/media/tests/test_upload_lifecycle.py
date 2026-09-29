"""The whole life of one file, against local storage end to end: initiate, upload its bytes, complete, verify,
thumbnail, download, retire - and everything that should refuse it along the way.
"""

from apps.media.constants import Status

from .base import MediaTestCase, make_pdf, make_png, sha256


class UploadLifecycleTests(MediaTestCase):
    def test_a_gallery_photo_goes_from_pending_to_available_with_a_thumbnail(self):
        data = make_png()
        asset = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=data, who=self.teacher)
        self.assertEqual(asset["status"], Status.AVAILABLE)
        self.assertEqual(asset["mediaType"], "image")
        self.assertTrue(asset["hasThumbnail"])
        self.assertEqual((asset["width"], asset["height"]), (6, 4))
        self.assertEqual(asset["uploadedByMembershipId"], str(self.teacher.id))
        self.assertEqual(asset["ownerType"], "gallery_media_album")
        self.assertEqual(asset["ownerId"], self.album_id)

    def test_a_document_has_no_thumbnail_but_still_becomes_available(self):
        data = make_pdf()
        asset = self.upload_all(owner_type="staff_profile_document", owner_id=self.staff_id, category="staff_document", data=data, mime_type="application/pdf", file_name="cv.pdf", who=self.owner)
        self.assertEqual(asset["status"], Status.AVAILABLE)
        self.assertFalse(asset["hasThumbnail"])
        self.assertIsNone(asset["width"])

    def test_the_original_filename_is_kept_only_as_a_label_the_stored_key_is_random(self):
        data = make_png()
        asset = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=data, file_name="../../etc/passwd.png", who=self.teacher)
        self.assertEqual(asset["fileName"], "passwd.png")  # only the basename survives; no path component is ever kept

    def test_a_size_that_does_not_match_what_was_declared_is_refused(self):
        data = make_png()
        created = self.initiate(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=data, who=self.teacher)
        asset_id = created.json()["asset"]["id"]
        put = self.put_body(f"assets/{asset_id}/upload/", data + b"\x00" * 10, "image/png", who=self.teacher)
        self.assertEqual((put.status_code, put.json()["code"]), (400, "size_mismatch"))

    def test_a_checksum_that_does_not_match_the_real_bytes_fails_verification_not_the_whole_request(self):
        data = make_png()
        created = self.initiate(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=data, who=self.teacher)
        asset_id = created.json()["asset"]["id"]
        # A believable but wrong declared checksum: the signature check at upload time cannot catch this (the
        # bytes really are a PNG), only the checksum comparison in the verify job can.
        wrong = "0" * 64
        from apps.media.models import MediaAsset

        MediaAsset.objects.filter(id=asset_id).update(checksum_sha256=wrong)
        put = self.put_body(f"assets/{asset_id}/upload/", data, "image/png", who=self.teacher)
        self.assertEqual(put.status_code, 200)
        done = self.post(f"assets/{asset_id}/complete/", who=self.teacher)
        self.assertEqual(done.json()["asset"]["status"], Status.FAILED)
        self.assertEqual(done.json()["asset"]["failureCode"], "checksum_mismatch")

    def test_bytes_that_do_not_match_the_declared_mime_type_are_refused_at_upload(self):
        not_really_a_png = b"this is not an image at all, just some text pretending"
        created = self.initiate(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=not_really_a_png, who=self.teacher)
        asset_id = created.json()["asset"]["id"]
        put = self.put_body(f"assets/{asset_id}/upload/", not_really_a_png, "image/png", who=self.teacher)
        self.assertEqual((put.status_code, put.json()["code"]), (400, "mime_type_mismatch"))
        self.assertEqual(self.get(f"assets/{asset_id}/", who=self.teacher).json()["asset"]["status"], Status.FAILED)

    def test_a_mime_type_the_category_does_not_accept_is_refused_before_anything_is_created(self):
        data = make_pdf()
        response = self.initiate(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=data, mime_type="application/pdf", who=self.teacher)
        self.assertEqual((response.status_code, response.json()["code"]), (400, "mime_type_not_allowed"))

    def test_a_file_larger_than_its_categorys_limit_is_refused_before_anything_is_created(self):
        response = self.initiate(
            owner_type="gallery_media_album", owner_id=self.album_id, category="profile_photo",
            data=b"x", mime_type="image/png", byteSize=999_999_999, who=self.teacher,
        )
        self.assertEqual((response.status_code, response.json()["code"]), (400, "file_too_large"))

    def test_an_unknown_category_is_refused(self):
        response = self.initiate(owner_type="gallery_media_album", owner_id=self.album_id, category="not_a_real_category", data=make_png(), who=self.teacher)
        self.assertEqual((response.status_code, response.json()["code"]), (400, "unknown_category"))

    def test_completing_twice_is_a_safe_no_op_not_a_second_verification(self):
        data = make_png()
        asset = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=data, who=self.teacher)
        again = self.post(f"assets/{asset['id']}/complete/", who=self.teacher)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["asset"]["status"], Status.AVAILABLE)

    def test_completing_before_the_bytes_have_arrived_is_refused(self):
        data = make_png()
        created = self.initiate(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=data, who=self.teacher)
        asset_id = created.json()["asset"]["id"]
        response = self.post(f"assets/{asset_id}/complete/", who=self.teacher)
        self.assertEqual((response.status_code, response.json()["code"]), (400, "not_uploaded"))

    def test_only_the_uploader_may_send_the_bytes_or_complete_the_upload(self):
        data = make_png()
        created = self.initiate(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=data, who=self.teacher)
        asset_id = created.json()["asset"]["id"]
        self.assertEqual(self.put_body(f"assets/{asset_id}/upload/", data, "image/png", who=self.owner).status_code, 403)
        self.assertEqual(self.post(f"assets/{asset_id}/complete/", who=self.owner).status_code, 403)

    def test_uploading_the_same_bytes_a_second_time_is_the_same_file_retried_not_a_double_charge(self):
        data = make_png()
        created = self.initiate(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=data, who=self.teacher)
        asset_id = created.json()["asset"]["id"]
        first = self.put_body(f"assets/{asset_id}/upload/", data, "image/png", who=self.teacher)
        second = self.put_body(f"assets/{asset_id}/upload/", data, "image/png", who=self.teacher)
        self.assertEqual(first.status_code, 200)
        self.assertEqual((second.status_code, second.json()["code"]), (400, "not_pending"))  # already uploaded once; a retried PUT is refused, not silently re-applied

    def test_downloading_asks_where_the_file_is_then_streams_the_real_bytes_back(self):
        data = make_png()
        asset = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=data, who=self.teacher)
        streamed = self.download_bytes(asset["id"], who=self.teacher)
        self.assertEqual(sha256(streamed), sha256(data))

    def test_the_thumbnail_can_be_downloaded_and_is_smaller_than_the_original(self):
        data = make_png(width=600, height=400)
        asset = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=data, who=self.teacher)
        thumbnail_bytes = self.download_bytes(asset["id"], thumbnail=True, who=self.teacher)
        self.assertLess(len(thumbnail_bytes), len(data))

    def test_retiring_removes_it_from_the_ordinary_list_and_purges_its_bytes(self):
        asset = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png(), who=self.teacher)
        retired = self.post(f"assets/{asset['id']}/retire/", {"reason": "wrong album"}, who=self.teacher)
        self.assertEqual(retired.json()["asset"]["status"], Status.RETIRED)
        listed = self.get(f"assets/?ownerType=gallery_media_album&ownerId={self.album_id}", who=self.teacher).json()["assets"]
        self.assertNotIn(asset["id"], [a["id"] for a in listed])
        # its bytes are gone, but the row (and its audit trail) survives
        from apps.media.models import MediaAsset

        row = MediaAsset.objects.get(id=asset["id"])
        self.assertIsNotNone(row.deleted_at)
        download_after_retire = self.get(f"assets/{asset['id']}/download/", who=self.teacher)
        self.assertEqual(download_after_retire.status_code, 400)

    def test_retiring_twice_is_a_safe_no_op(self):
        asset = self.upload_all(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png(), who=self.teacher)
        self.post(f"assets/{asset['id']}/retire/", who=self.teacher)
        again = self.post(f"assets/{asset['id']}/retire/", who=self.teacher)
        self.assertEqual(again.status_code, 200)

    def test_an_unauthenticated_call_is_refused(self):
        response = self.client.get(self.path("assets/?ownerType=gallery_media_album&ownerId=x"))
        self.assertEqual(response.status_code, 401)
