from django.contrib.auth import get_user_model
from rest_framework.test import APIClient, APITestCase

from apps.schools.models import Membership, Role, School

User = get_user_model()
PASSWORD = "a-long-test-password-1"


class AuthAndMembershipTests(APITestCase):
    def setUp(self):
        self.school = School.objects.create(name="BrightGate Academy", slug="brightgate")
        self.other = School.objects.create(name="Other School", slug="other")
        self.user = User.objects.create_user("owner@school.ng", PASSWORD, first_name="Ibrahim")
        self.owner = Membership.objects.create(user=self.user, school=self.school, role=Role.PROPRIETOR)

    def sign_in(self, email="owner@school.ng", password=PASSWORD):
        return self.client.post(
            "/api/v1/auth/token/", {"email": email, "password": password}, format="json"
        )

    def test_health_needs_no_login(self):
        response = APIClient().get("/api/v1/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_sign_in_returns_tokens_and_wrong_password_is_refused(self):
        ok = self.sign_in()
        self.assertEqual(ok.status_code, 200)
        self.assertIn("access", ok.json())
        self.assertIn("refresh", ok.json())
        self.assertEqual(self.sign_in(password="wrong").status_code, 401)
        self.assertEqual(self.sign_in(email="nobody@school.ng").status_code, 401)

    def test_email_is_stored_lower_case_and_unique(self):
        user = User.objects.create_user("Mixed@School.NG", PASSWORD)
        self.assertEqual(user.email, "mixed@school.ng")
        with self.assertRaises(Exception):
            User.objects.create_user("mixed@school.ng", PASSWORD)

    def test_me_requires_a_token(self):
        self.assertEqual(APIClient().get("/api/v1/me/").status_code, 401)

    def test_me_lists_memberships_in_the_apps_shape(self):
        token = self.sign_in().json()["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        body = self.client.get("/api/v1/me/").json()
        self.assertEqual(body["email"], "owner@school.ng")
        self.assertEqual(
            body["memberships"],
            [
                {
                    "id": str(self.owner.id),
                    "schoolId": str(self.school.id),
                    "schoolName": "BrightGate Academy",
                    "role": "proprietor",
                }
            ],
        )

    def test_me_hides_inactive_memberships_and_schools(self):
        Membership.objects.create(user=self.user, school=self.other, role=Role.TEACHER, is_active=False)
        self.school.is_active = False
        self.school.save()
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get("/api/v1/me/").json()["memberships"], [])

    def test_one_person_can_hold_several_roles(self):
        Membership.objects.create(user=self.user, school=self.school, role=Role.PARENT)
        self.client.force_authenticate(self.user)
        roles = {m["role"] for m in self.client.get("/api/v1/me/").json()["memberships"]}
        self.assertEqual(roles, {"proprietor", "parent"})
        with self.assertRaises(Exception):
            Membership.objects.create(user=self.user, school=self.school, role=Role.PARENT)
