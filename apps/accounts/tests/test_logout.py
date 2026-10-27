from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.schools.models import Membership, Role, School

User = get_user_model()
PASSWORD = "a-long-test-password-1"

TOKEN_URL = "/api/v1/auth/token/"
REFRESH_URL = "/api/v1/auth/token/refresh/"
LOGOUT_URL = "/api/v1/auth/logout/"


class LogoutTests(APITestCase):
    def setUp(self):
        self.school = School.objects.create(name="BrightGate Academy", slug="brightgate")
        self.user = User.objects.create_user("owner@school.ng", PASSWORD, first_name="Ibrahim")
        Membership.objects.create(user=self.user, school=self.school, role=Role.PROPRIETOR)
        self.other_user = User.objects.create_user("other@school.ng", PASSWORD, first_name="Amina")
        Membership.objects.create(user=self.other_user, school=self.school, role=Role.TEACHER)

    def sign_in(self, email="owner@school.ng"):
        return self.client.post(TOKEN_URL, {"email": email, "password": PASSWORD}, format="json").json()

    def test_logout_requires_authentication(self):
        response = self.client.post(LOGOUT_URL, {"refresh": "whatever"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_a_missing_refresh_token_is_refused(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(LOGOUT_URL, {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_a_malformed_refresh_token_is_refused(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(LOGOUT_URL, {"refresh": "not-a-real-token"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_logging_out_blacklists_this_refresh_token(self):
        tokens = self.sign_in()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        response = self.client.post(LOGOUT_URL, {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"loggedOut": True})

        self.client.credentials()
        refreshed = self.client.post(REFRESH_URL, {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(refreshed.status_code, 401)

    def test_logging_out_with_someone_elses_refresh_token_is_refused_and_changes_nothing(self):
        mine = self.sign_in()
        theirs = self.sign_in(email="other@school.ng")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {mine['access']}")
        response = self.client.post(LOGOUT_URL, {"refresh": theirs["refresh"]}, format="json")
        self.assertEqual(response.status_code, 400)

        self.client.credentials()
        still_works = self.client.post(REFRESH_URL, {"refresh": theirs["refresh"]}, format="json")
        self.assertEqual(still_works.status_code, 200)

    def test_logging_out_of_one_session_leaves_another_session_of_the_same_account_alone(self):
        session_a = self.sign_in()
        session_b = self.sign_in()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {session_a['access']}")
        self.client.post(LOGOUT_URL, {"refresh": session_a["refresh"]}, format="json")

        self.client.credentials()
        still_works = self.client.post(REFRESH_URL, {"refresh": session_b["refresh"]}, format="json")
        self.assertEqual(still_works.status_code, 200)

    def test_rotation_blacklists_the_old_refresh_token_too(self):
        tokens = self.sign_in()
        first_refresh = self.client.post(REFRESH_URL, {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(first_refresh.status_code, 200)

        replayed = self.client.post(REFRESH_URL, {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(replayed.status_code, 401)

        new_refresh = first_refresh.json()["refresh"]
        second_refresh = self.client.post(REFRESH_URL, {"refresh": new_refresh}, format="json")
        self.assertEqual(second_refresh.status_code, 200)

    def test_logout_does_not_touch_credential_version_or_other_sessions(self):
        tokens = self.sign_in()
        before_version = self.user.credential_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        self.client.post(LOGOUT_URL, {"refresh": tokens["refresh"]}, format="json")
        self.user.refresh_from_db()
        self.assertEqual(self.user.credential_version, before_version)

