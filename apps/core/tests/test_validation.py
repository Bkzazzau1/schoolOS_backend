from django.test import SimpleTestCase

from apps.core.errors import Rejected
from apps.core.validation import boolean, choice, integer, is_email, string_list, text


class ValidationTests(SimpleTestCase):
    def test_text(self):
        self.assertEqual(text({"a": "  hi  "}, "a"), "hi")
        self.assertEqual(text({}, "a", required=False), "")
        self.assertEqual(text({"a": None}, "a", required=False), "")
        for bad in [{}, {"a": ""}, {"a": "   "}, {"a": 5}, {"a": "x" * 201}]:
            with self.assertRaises(Rejected, msg=bad):
                text(bad, "a")

    def test_integer_rejects_everything_that_is_not_a_whole_number(self):
        self.assertEqual(integer({"a": 5}, "a"), 5)
        for bad in [True, False, "5", 5.0, None, -1, 11**10]:
            with self.assertRaises(Rejected, msg=repr(bad)):
                integer({"a": bad}, "a")
        with self.assertRaises(Rejected):
            integer({}, "a")
        self.assertEqual(integer({"a": 0}, "a", minimum=0, maximum=0), 0)

    def test_boolean_is_strict(self):
        self.assertIs(boolean({"a": True}, "a"), True)
        for bad in [1, 0, "true", None]:
            with self.assertRaises(Rejected):
                boolean({"a": bad}, "a")

    def test_choice(self):
        self.assertEqual(choice("x", {"x", "y"}, "thing"), "x")
        with self.assertRaises(Rejected):
            choice("z", {"x", "y"}, "thing")
        with self.assertRaises(Rejected):
            choice(None, {"x"}, "thing")

    def test_string_list(self):
        self.assertEqual(string_list({"a": ["b", "a", "b"]}, "a", allowed={"a", "b"}), ["a", "b"])
        for bad in [{"a": []}, {"a": "a"}, {"a": [1]}, {"a": ["c"]}, {}]:
            with self.assertRaises(Rejected, msg=bad):
                string_list(bad, "a", allowed={"a", "b"})
        with self.assertRaises(Rejected):
            string_list({"a": ["a", "b"]}, "a", allowed={"a", "b"}, max_items=1)

    def test_email(self):
        self.assertTrue(is_email("a@b.ng"))
        for bad in ["", "a", "a@b", "a b@c.ng", "@b.ng"]:
            self.assertFalse(is_email(bad), bad)
