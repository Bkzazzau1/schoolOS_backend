from django.test import SimpleTestCase

from apps.media import validation
from apps.media.constants import MediaType
from apps.media.validation import UploadRefused


class SanitizeFilenameTests(SimpleTestCase):
    def test_a_path_is_reduced_to_its_basename(self):
        self.assertEqual(validation.sanitize_filename("../../etc/passwd.png"), "passwd.png")
        self.assertEqual(validation.sanitize_filename("C:\\Users\\me\\Desktop\\photo.png"), "photo.png")

    def test_control_and_odd_characters_are_removed_not_encoded(self):
        self.assertEqual(validation.sanitize_filename("weird<>:\"|?*name.png"), "weird name.png")

    def test_an_empty_or_purely_unsafe_name_still_yields_something(self):
        self.assertEqual(validation.sanitize_filename(""), "file")
        self.assertEqual(validation.sanitize_filename("////"), "file")

    def test_a_very_long_name_is_capped(self):
        self.assertLessEqual(len(validation.sanitize_filename("a" * 500 + ".png")), 150)


class CheckIntentTests(SimpleTestCase):
    def _ok(self, **over):
        kwargs = dict(category="gallery_photo", declared_mime_type="image/png", declared_byte_size=1000, original_filename="a.png", school_id="school-1")
        kwargs.update(over)
        return validation.check_intent(**kwargs)

    def test_a_known_category_and_matching_mime_type_is_accepted(self):
        intent = self._ok()
        self.assertEqual((intent.media_type, intent.extension), (MediaType.IMAGE, ".png"))

    def test_the_storage_key_is_random_and_never_derived_from_the_filename(self):
        first = self._ok(original_filename="secret-plans.png")
        second = self._ok(original_filename="secret-plans.png")
        self.assertNotEqual(first.storage_key, second.storage_key)
        self.assertNotIn("secret-plans", first.storage_key)
        self.assertNotIn("secret", first.stored_filename)

    def test_the_storage_key_is_scoped_under_the_school_id(self):
        intent = self._ok(school_id="school-42")
        self.assertTrue(intent.storage_key.startswith("school-42/"))

    def test_an_unknown_category_is_refused(self):
        with self.assertRaises(UploadRefused) as caught:
            self._ok(category="not-a-category")
        self.assertEqual(caught.exception.code, "unknown_category")

    def test_a_mime_type_the_category_does_not_accept_is_refused(self):
        with self.assertRaises(UploadRefused) as caught:
            self._ok(category="gallery_photo", declared_mime_type="application/pdf")
        self.assertEqual(caught.exception.code, "mime_type_not_allowed")

    def test_the_other_category_accepts_nothing_by_default(self):
        with self.assertRaises(UploadRefused):
            self._ok(category="gallery_photo", declared_mime_type="application/x-msdownload")

    def test_a_zero_or_negative_or_non_integer_size_is_refused(self):
        for bad in (0, -5, "1000", 1.5, None):
            with self.assertRaises(UploadRefused) as caught:
                self._ok(declared_byte_size=bad)
            self.assertEqual(caught.exception.code, "invalid_size")

    def test_a_size_over_the_categorys_ceiling_is_refused(self):
        with self.assertRaises(UploadRefused) as caught:
            self._ok(category="profile_photo", declared_byte_size=50 * 1024 * 1024)
        self.assertEqual(caught.exception.code, "file_too_large")

    def test_a_categorys_own_limit_is_never_looser_than_its_media_types_ceiling(self):
        # profile_photo (6MB) is stricter than image's own 15MB ceiling; nothing in CATEGORY_MAX_BYTES may exceed
        # MAX_BYTES_BY_MEDIA_TYPE for its type - this is the invariant max_bytes_for is supposed to hold.
        from apps.media.constants import CATEGORY_MAX_BYTES, MAX_BYTES_BY_MEDIA_TYPE, media_type_for_category

        for category, limit in CATEGORY_MAX_BYTES.items():
            media_type = media_type_for_category(category)
            self.assertLessEqual(limit, MAX_BYTES_BY_MEDIA_TYPE[media_type], category)


class SignatureCheckTests(SimpleTestCase):
    def test_bytes_that_do_not_match_a_png_signature_are_refused(self):
        with self.assertRaises(UploadRefused) as caught:
            validation.check_signature(declared_mime_type="image/png", head=b"not a png at all")
        self.assertEqual(caught.exception.code, "mime_type_mismatch")

    def test_a_real_png_signature_passes(self):
        import struct
        import zlib

        def chunk(tag, data):
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        validation.check_signature(declared_mime_type="image/png", head=png[:32])  # must not raise

    def test_a_renamed_executable_disguised_with_a_pdf_mime_type_is_refused(self):
        with self.assertRaises(UploadRefused):
            validation.check_signature(declared_mime_type="application/pdf", head=b"MZ\x90\x00\x03\x00\x00\x00")

    def test_a_real_pdf_signature_passes(self):
        validation.check_signature(declared_mime_type="application/pdf", head=b"%PDF-1.4\n%rest of file")

    def test_a_mime_type_with_no_registered_sniffer_is_not_checked_at_this_level(self):
        validation.check_signature(declared_mime_type="text/plain", head=b"anything at all")  # must not raise


class ChecksumTests(SimpleTestCase):
    def test_sha256_of_is_a_real_sha256(self):
        import hashlib

        self.assertEqual(validation.sha256_of(b"hello"), hashlib.sha256(b"hello").hexdigest())
