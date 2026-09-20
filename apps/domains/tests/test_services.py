from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from apps.domains import services
from apps.domains.hostnames import DomainError
from apps.domains.models import SchoolDomain
from apps.schools.models import School


@override_settings(PLATFORM_DOMAIN="schoolos.ng")
class PlatformDomainTests(TestCase):
    def test_a_new_school_gets_a_verified_primary_platform_subdomain(self):
        school = School.objects.create(name="BrightGate", slug="brightgate")
        domain = school.domains.get()
        self.assertEqual(domain.host, "brightgate.schoolos.ng")
        self.assertEqual((domain.kind, domain.status, domain.is_primary), ("platform", "verified", True))
        self.assertIsNotNone(domain.verified_at)
        self.assertEqual(domain.verification_token, "")

    def test_it_is_safe_to_ask_again(self):
        school = School.objects.create(name="BrightGate", slug="brightgate")
        self.assertEqual(services.ensure_platform_domain(school).pk, services.ensure_platform_domain(school).pk)
        self.assertEqual(school.domains.count(), 1)

    def test_a_school_whose_short_name_is_reserved_is_still_created_but_has_no_address(self):
        school = School.objects.create(name="Admin School", slug="admin")
        self.assertEqual(school.domains.count(), 0)
        with self.assertRaises(DomainError):
            services.ensure_platform_domain(school)

    def test_it_never_takes_over_another_schools_address(self):
        first = School.objects.create(name="A", slug="brightgate")
        second = School.objects.create(name="B", slug="other")
        SchoolDomain.objects.filter(school=second).delete()
        SchoolDomain.objects.filter(school=first).update(host="other.schoolos.ng")
        with self.assertRaises(DomainError):
            services.ensure_platform_domain(second)

    @override_settings(PLATFORM_DOMAIN="")
    def test_with_no_platform_domain_configured_nothing_is_created(self):
        school = School.objects.create(name="BrightGate", slug="brightgate")
        self.assertEqual(school.domains.count(), 0)
        self.assertIsNone(services.ensure_platform_domain(school))


@override_settings(PLATFORM_DOMAIN="schoolos.ng")
class CustomDomainTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="BrightGate", slug="brightgate")
        self.platform = self.school.domains.get()
        self.records = {}

    def resolver(self, name):
        return self.records.get(name, [])

    def test_a_custom_domain_starts_pending_with_a_token_and_is_not_used_yet(self):
        domain = services.add_custom_domain(self.school, "  School.BrightGate.NG ")
        self.assertEqual((domain.host, domain.kind, domain.status), ("school.brightgate.ng", "custom", "pending"))
        self.assertGreaterEqual(len(domain.verification_token), 24)
        self.assertEqual(domain.dns_record_name, "_schoolos-verify.school.brightgate.ng")
        self.assertIsNone(services.resolve_host("school.brightgate.ng"))
        self.assertEqual(services.link_host(self.school), "brightgate.schoolos.ng")

    def test_tokens_are_different_for_every_domain(self):
        a = services.add_custom_domain(self.school, "a.brightgate.ng")
        b = services.add_custom_domain(self.school, "b.brightgate.ng")
        self.assertNotEqual(a.verification_token, b.verification_token)

    def test_bad_taken_and_platform_owned_domains_are_refused(self):
        services.add_custom_domain(self.school, "brightgate.ng")
        other = School.objects.create(name="Other", slug="other")
        for host in ["https://x.ng", "localhost", "1.2.3.4"]:
            with self.assertRaises(DomainError, msg=host):
                services.add_custom_domain(self.school, host)
        with self.assertRaises(DomainError):
            services.add_custom_domain(other, "brightgate.ng")  # already registered
        with self.assertRaises(DomainError):
            services.add_custom_domain(other, "brightgate.ng ")  # same after trimming
        for host in ["schoolos.ng", "other.schoolos.ng", "x.y.schoolos.ng"]:
            with self.assertRaises(DomainError, msg=host):
                services.add_custom_domain(other, host)

    def test_verification_needs_the_token_in_dns(self):
        domain = services.add_custom_domain(self.school, "brightgate.ng")
        with self.assertRaises(DomainError) as caught:
            services.verify_custom_domain(domain, self.resolver)
        self.assertIn("_schoolos-verify.brightgate.ng", caught.exception.message)
        self.assertIn(domain.verification_token, caught.exception.message)
        # A wrong value does not count, and neither does the token at the wrong name.
        self.records["_schoolos-verify.brightgate.ng"] = ["wrong"]
        self.records["brightgate.ng"] = [domain.verification_token]
        with self.assertRaises(DomainError):
            services.verify_custom_domain(domain, self.resolver)
        domain.refresh_from_db()
        self.assertEqual(domain.status, "pending")

        self.records["_schoolos-verify.brightgate.ng"] = ["v=spf1 -all", domain.verification_token]
        services.verify_custom_domain(domain, self.resolver)
        domain.refresh_from_db()
        self.assertEqual(domain.status, "verified")
        self.assertIsNotNone(domain.verified_at)
        self.assertEqual(services.resolve_host("brightgate.ng").school, self.school)

    def test_verifying_twice_and_verifying_the_wrong_kind_are_handled(self):
        domain = services.add_custom_domain(self.school, "brightgate.ng")
        self.records[domain.dns_record_name] = [domain.verification_token]
        services.verify_custom_domain(domain, self.resolver)
        first = domain.verified_at
        services.verify_custom_domain(domain, lambda name: [])  # already verified: no DNS needed
        domain.refresh_from_db()
        self.assertEqual(domain.verified_at, first)
        with self.assertRaises(DomainError):
            services.verify_custom_domain(self.platform, self.resolver)

    def test_a_disabled_domain_cannot_be_verified(self):
        domain = services.add_custom_domain(self.school, "brightgate.ng")
        services.disable(domain)
        self.records[domain.dns_record_name] = [domain.verification_token]
        with self.assertRaises(DomainError):
            services.verify_custom_domain(domain, self.resolver)

    def test_the_default_dns_lookup_returns_nothing_rather_than_crashing(self):
        # No network is needed: an invalid name simply has no records.
        self.assertEqual(services.dns_txt_records("_schoolos-verify.invalid.invalid"), [])


@override_settings(PLATFORM_DOMAIN="schoolos.ng")
class PrimaryAndResolveTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="BrightGate", slug="brightgate")
        self.platform = self.school.domains.get()
        self.custom = services.add_custom_domain(self.school, "brightgate.ng")

    def verify(self, domain):
        services.verify_custom_domain(domain, lambda name: [domain.verification_token])

    def test_only_a_verified_domain_can_be_primary_and_only_one_at_a_time(self):
        with self.assertRaises(DomainError):
            services.set_primary(self.custom)
        self.verify(self.custom)
        services.set_primary(self.custom)
        self.platform.refresh_from_db()
        self.assertEqual((self.platform.is_primary, self.custom.is_primary), (False, True))
        self.assertEqual(services.link_host(self.school), "brightgate.ng")
        self.assertEqual(self.school.domains.filter(is_primary=True).count(), 1)

    def test_the_database_itself_refuses_two_primaries_or_an_unverified_primary(self):
        self.verify(self.custom)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SchoolDomain.objects.filter(pk=self.custom.pk).update(is_primary=True)
        other = services.add_custom_domain(self.school, "other.ng")
        with self.assertRaises(IntegrityError), transaction.atomic():
            SchoolDomain.objects.filter(pk=other.pk).update(is_primary=True)

    def test_the_primary_domain_cannot_be_disabled_until_another_is_chosen(self):
        with self.assertRaises(DomainError):
            services.disable(self.platform)
        self.verify(self.custom)
        services.set_primary(self.custom)
        services.disable(self.platform)
        self.assertIsNone(services.resolve_host("brightgate.schoolos.ng"))

    def test_only_verified_domains_of_active_schools_resolve(self):
        self.assertEqual(services.resolve_host("BRIGHTGATE.schoolos.ng").school, self.school)  # case-insensitive
        self.assertIsNone(services.resolve_host("brightgate.ng"))          # pending
        self.assertIsNone(services.resolve_host("unknown.schoolos.ng"))    # unknown
        for junk in ["", "localhost", "https://brightgate.schoolos.ng", "brightgate.schoolos.ng:99"]:
            self.assertIsNone(services.resolve_host(junk), junk)
        self.school.is_active = False
        self.school.save()
        self.assertIsNone(services.resolve_host("brightgate.schoolos.ng"))

    def test_two_schools_never_resolve_to_each_other(self):
        other = School.objects.create(name="Other", slug="other")
        self.assertEqual(services.resolve_host("other.schoolos.ng").school, other)
        self.assertEqual(services.resolve_host("brightgate.schoolos.ng").school, self.school)

    def test_a_school_with_no_verified_primary_has_no_link_host(self):
        bare = School.objects.create(name="Bare", slug="admin")  # reserved: no platform domain
        self.assertIsNone(services.link_host(bare))

    def test_saving_an_existing_domain_does_not_revalidate_its_host(self):
        # If the platform domain is changed later, existing rows must stay editable.
        with override_settings(PLATFORM_DOMAIN="other.example"):
            self.platform.status = "verified"
            self.platform.save()
        self.platform.refresh_from_db()
        self.assertEqual(self.platform.host, "brightgate.schoolos.ng")
