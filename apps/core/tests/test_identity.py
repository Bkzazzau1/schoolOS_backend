from django.test import SimpleTestCase

from apps.core.identity import normalize_name, normalize_nin, normalize_phone


class IdentityTests(SimpleTestCase):
    def test_every_way_of_writing_a_number_is_the_same_number(self):
        for value in ["08031234567", "0803 123 4567", "+234 803 123 4567", "2348031234567",
                      "0803-123-4567", "(0803) 123 4567", " 0803.123.4567 "]:
            self.assertEqual(normalize_phone(value), "08031234567", value)

    def test_things_that_are_not_nigerian_mobile_numbers_are_refused(self):
        for value in ["", None, "123", "0803123456", "080312345678", "0603 123 4567", "abc", "+44 7911 123456",
                      "+2348031234567890", "234803123456"]:
            self.assertIsNone(normalize_phone(value), repr(value))

    def test_every_network_prefix_shape_is_accepted(self):
        for value in ["07012345678", "08012345678", "09012345678", "08112345678", "07112345678"]:
            self.assertEqual(normalize_phone(value), value)

    def test_nin(self):
        self.assertEqual(normalize_nin("123 4567 8901"), "12345678901")
        self.assertEqual(normalize_nin("123-4567-8901"), "12345678901")
        for value in ["", None, "1234567890", "123456789012", "1234567890a"]:
            self.assertIsNone(normalize_nin(value), repr(value))

    def test_names_are_compared_ignoring_case_spacing_and_punctuation(self):
        self.assertEqual(normalize_name("  Mrs.  Amina   YUSUF "), "mrs amina yusuf")
        self.assertEqual(normalize_name(None), "")
        self.assertEqual(normalize_name("Ali-Baba, Jr."), normalize_name("ali baba jr"))
