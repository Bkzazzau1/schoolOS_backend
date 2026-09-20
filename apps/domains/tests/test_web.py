from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings

from apps.domains import services
from apps.domains.middleware import SchoolHostMiddleware
from apps.schools.models import School

FINGERPRINT = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"
ASSETLINKS = "/.well-known/assetlinks.json"


@override_settings(
    PLATFORM_DOMAIN="schoolos.ng",
    ALLOWED_HOSTS=["*"],
    ANDROID_APP_PACKAGE="ng.schoolos.app",
    ANDROID_CERT_SHA256=[FINGERPRINT],
)
class WebTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="BrightGate", slug="brightgate")
        self.custom = services.add_custom_domain(self.school, "brightgate.ng")
        services.verify_custom_domain(self.custom, lambda name: [self.custom.verification_token])

    def get(self, path, host):
        return self.client.get(path, headers={"host": host})

    # -- middleware -----------------------------------------------------------

    def test_the_middleware_identifies_the_school_from_the_host(self):
        seen = {}

        def view(request):
            seen["domain"], seen["school"] = request.school_domain, request.school
            return None

        for host, expected in [
            ("brightgate.schoolos.ng", self.school),
            ("BrightGate.SchoolOS.ng:8443", self.school),  # case and port ignored
            ("brightgate.ng", self.school),
            ("unknown.schoolos.ng", None),
            ("evil.example.com", None),
            ("", None),
        ]:
            request = RequestFactory().get("/", HTTP_HOST=host) if host else RequestFactory().get("/")
            if not host:
                request.META.pop("HTTP_HOST", None)
            SchoolHostMiddleware(view)(request)
            self.assertEqual(seen["school"], expected, host)
            self.assertEqual(seen["domain"] is None, expected is None, host)

    def test_a_forwarded_host_header_is_not_trusted(self):
        request = RequestFactory().get("/", HTTP_HOST="evil.example.com", HTTP_X_FORWARDED_HOST="brightgate.schoolos.ng")
        SchoolHostMiddleware(lambda r: None)(request)
        self.assertIsNone(request.school)

    def test_the_api_still_works_on_any_host(self):
        for host in ["brightgate.schoolos.ng", "api.schoolos.ng", "unknown.example.com"]:
            self.assertEqual(self.get("/api/v1/health/", host).status_code, 200, host)

    # -- app links --------------------------------------------------------------

    def test_a_platform_subdomain_vouches_for_the_app(self):
        response = self.get(ASSETLINKS, "brightgate.schoolos.ng")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(
            response.json(),
            [{
                "relation": ["delegate_permission/common.handle_all_urls"],
                "target": {
                    "namespace": "android_app",
                    "package_name": "ng.schoolos.app",
                    "sha256_cert_fingerprints": [FINGERPRINT],
                },
            }],
        )

    def test_a_custom_domain_does_not_vouch_for_the_app(self):
        self.assertEqual(self.get(ASSETLINKS, "brightgate.ng").status_code, 404)

    def test_unknown_pending_and_disabled_hosts_do_not_vouch_for_the_app(self):
        pending = services.add_custom_domain(self.school, "pending.ng")
        self.assertEqual(self.get(ASSETLINKS, pending.host).status_code, 404)
        self.assertEqual(self.get(ASSETLINKS, "unknown.schoolos.ng").status_code, 404)
        self.assertEqual(self.get(ASSETLINKS, "evil.example.com").status_code, 404)
        services.set_primary(self.custom)
        services.disable(self.school.domains.get(kind="platform"))
        self.assertEqual(self.get(ASSETLINKS, "brightgate.schoolos.ng").status_code, 404)

    def test_it_says_nothing_until_the_app_is_configured(self):
        for change in [{"ANDROID_APP_PACKAGE": ""}, {"ANDROID_CERT_SHA256": []}]:
            with override_settings(**change):
                self.assertEqual(self.get(ASSETLINKS, "brightgate.schoolos.ng").status_code, 404, change)


@override_settings(PLATFORM_DOMAIN="schoolos.ng")
class AdminTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("root@schoolos.ng", "a-long-test-password-1")
        self.client.force_login(self.user)
        self.school = School.objects.create(name="BrightGate", slug="brightgate")
        self.custom = services.add_custom_domain(self.school, "brightgate.ng")
        services.verify_custom_domain(self.custom, lambda name: [self.custom.verification_token])

    def run_action(self, action, *domains):
        return self.client.post(
            "/admin/domains/schooldomain/",
            {"action": action, "_selected_action": [d.pk for d in domains]},
            follow=True,
        )

    def test_the_domain_pages_load_and_show_dns_instructions(self):
        self.assertEqual(self.client.get("/admin/domains/schooldomain/").status_code, 200)
        page = self.client.get(f"/admin/domains/schooldomain/{self.custom.pk}/change/")
        self.assertContains(page, "_schoolos-verify.brightgate.ng")
        self.assertContains(page, self.custom.verification_token)

    def test_make_primary_and_disable_work_from_the_admin(self):
        response = self.run_action("make_primary", self.custom)
        self.assertContains(response, "is now primary")
        self.custom.refresh_from_db()
        self.assertTrue(self.custom.is_primary)
        # The platform domain is no longer primary, so it can now be disabled.
        platform = self.school.domains.get(kind="platform")
        self.assertContains(self.run_action("disable_domains", platform), "disabled")
        # The primary one cannot be, and the reason is shown.
        self.assertContains(self.run_action("disable_domains", self.custom), "Make another domain primary")
        self.custom.refresh_from_db()
        self.assertEqual((self.custom.status, self.custom.is_primary), ("verified", True))

    def test_the_admin_form_refuses_a_custom_domain_inside_the_platform(self):
        response = self.client.post(
            "/admin/domains/schooldomain/add/",
            {"school": self.school.pk, "host": "other.schoolos.ng", "kind": "custom", "status": "pending"},
        )
        self.assertEqual(response.status_code, 200)  # form redisplayed with an error
        self.assertContains(response, "belongs to the platform")
        self.assertFalse(self.school.domains.filter(host="other.schoolos.ng").exists())
