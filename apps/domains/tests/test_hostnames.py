from django.test import SimpleTestCase, override_settings

from apps.domains.hostnames import (
    DomainError,
    normalize_host,
    platform_host_for,
    validate_for_kind,
)


class NormalizeHostTests(SimpleTestCase):
    def test_good_hosts_are_lower_cased_and_trimmed(self):
        self.assertEqual(normalize_host("  School.Example.COM. "), "school.example.com")
        self.assertEqual(normalize_host("a-b.c1.ng"), "a-b.c1.ng")

    def test_anything_that_is_not_a_plain_host_is_refused(self):
        bad = [
            "", "   ", "localhost", "school",
            "https://school.example.com", "school.example.com/login", "school.example.com:8000",
            "user@school.example.com", "school example.com", "sch_ool.example.com",
            "-school.example.com", "school-.example.com", ".example.com", "a..com",
            "192.168.0.1", "1.2.3.4", "school.example.com/../x",
            "a" * 64 + ".com", ("a." * 130) + "com",
            "schöol.example.com",  # non-ASCII must be sent as punycode
        ]
        for value in bad:
            with self.assertRaises(DomainError, msg=repr(value)):
                normalize_host(value)

    def test_the_error_message_says_what_to_do(self):
        with self.assertRaises(DomainError) as caught:
            normalize_host("https://school.example.com")
        self.assertIn("https://", caught.exception.message)


@override_settings(PLATFORM_DOMAIN="schoolos.ng")
class PlatformHostTests(SimpleTestCase):
    def test_a_school_gets_its_short_name_under_the_platform_domain(self):
        self.assertEqual(platform_host_for("brightgate"), "brightgate.schoolos.ng")
        self.assertEqual(platform_host_for("Bright-Gate-2"), "bright-gate-2.schoolos.ng")

    def test_reserved_and_invalid_short_names_are_refused(self):
        for slug in ["www", "api", "admin", "mail", "login", "", "-x", "x-", "a_b", "a.b", "x" * 64]:
            with self.assertRaises(DomainError, msg=slug):
                platform_host_for(slug)

    @override_settings(PLATFORM_DOMAIN="")
    def test_without_a_platform_domain_there_is_no_platform_host(self):
        with self.assertRaises(DomainError):
            platform_host_for("brightgate")


@override_settings(PLATFORM_DOMAIN="schoolos.ng")
class ValidateForKindTests(SimpleTestCase):
    def test_a_platform_domain_is_exactly_one_name_under_the_platform_domain(self):
        self.assertEqual(validate_for_kind("BrightGate.schoolos.ng", "platform"), "brightgate.schoolos.ng")
        for bad in ["schoolos.ng", "a.b.schoolos.ng", "brightgate.example.com", "www.schoolos.ng", "brightgatexschoolos.ng"]:
            with self.assertRaises(DomainError, msg=bad):
                validate_for_kind(bad, "platform")

    def test_a_custom_domain_can_never_be_inside_the_platform_domain(self):
        self.assertEqual(validate_for_kind("brightgate.ng", "custom"), "brightgate.ng")
        self.assertEqual(validate_for_kind("school.brightgate.ng", "custom"), "school.brightgate.ng")
        # A school must not claim the platform itself or another school's address.
        for bad in ["schoolos.ng", "other.schoolos.ng", "x.y.schoolos.ng"]:
            with self.assertRaises(DomainError, msg=bad):
                validate_for_kind(bad, "custom")

    def test_a_look_alike_is_not_mistaken_for_the_platform(self):
        # "notschoolos.ng" ends with the same letters but is a different domain.
        self.assertEqual(validate_for_kind("notschoolos.ng", "custom"), "notschoolos.ng")

    @override_settings(PLATFORM_DOMAIN="")
    def test_platform_domains_need_the_platform_domain_configured(self):
        with self.assertRaises(DomainError):
            validate_for_kind("brightgate.schoolos.ng", "platform")
        self.assertEqual(validate_for_kind("brightgate.ng", "custom"), "brightgate.ng")
