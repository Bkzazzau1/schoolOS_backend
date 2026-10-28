from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.accounts.models import DevicePlatform, DeviceToken
from apps.schools.models import Membership, Role, School

User = get_user_model()
PASSWORD = "a-long-test-password-1"

REGISTER_URL = "/api/v1/auth/device-tokens/"
UNREGISTER_URL = "/api/v1/auth/device-tokens/unregister/"


class DeviceTokenTests(APITestCase):
    def setUp(self):
        self.school = School.objects.create(name="BrightGate Academy", slug="brightgate")
        self.user = User.objects.create_user("owner@school.ng", PASSWORD, first_name="Ibrahim")
        Membership.objects.create(user=self.user, school=self.school, role=Role.PROPRIETOR)
        self.other_user = User.objects.create_user("other@school.ng", PASSWORD, first_name="Amina")

    def test_registering_requires_authentication(self):
        response = self.client.post(REGISTER_URL, {"token": "tok-1", "platform": "android"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_registering_a_device_token_creates_it(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(REGISTER_URL, {"token": "tok-1", "platform": "android"}, format="json")
        self.assertEqual(response.status_code, 200)
        token = DeviceToken.objects.get(token="tok-1")
        self.assertEqual(token.user, self.user)
        self.assertEqual(token.platform, DevicePlatform.ANDROID)

    def test_a_missing_token_is_refused(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(REGISTER_URL, {"platform": "android"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_an_invalid_platform_is_refused(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(REGISTER_URL, {"token": "tok-1", "platform": "blackberry"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(DeviceToken.objects.exists())

    def test_registering_the_same_token_again_for_the_same_account_updates_it_in_place(self):
        self.client.force_authenticate(self.user)
        self.client.post(REGISTER_URL, {"token": "tok-1", "platform": "android"}, format="json")
        self.client.post(REGISTER_URL, {"token": "tok-1", "platform": "ios"}, format="json")
        self.assertEqual(DeviceToken.objects.count(), 1)
        self.assertEqual(DeviceToken.objects.get().platform, DevicePlatform.IOS)

    def test_a_shared_device_signing_into_a_different_account_reassigns_its_token(self):
        self.client.force_authenticate(self.user)
        self.client.post(REGISTER_URL, {"token": "shared-tok", "platform": "android"}, format="json")

        self.client.force_authenticate(self.other_user)
        response = self.client.post(REGISTER_URL, {"token": "shared-tok", "platform": "android"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DeviceToken.objects.count(), 1)
        self.assertEqual(DeviceToken.objects.get().user, self.other_user)

    def test_unregistering_requires_authentication(self):
        response = self.client.post(UNREGISTER_URL, {"token": "tok-1"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_unregistering_removes_the_token(self):
        DeviceToken.objects.create(user=self.user, platform=DevicePlatform.ANDROID, token="tok-1")
        self.client.force_authenticate(self.user)
        response = self.client.post(UNREGISTER_URL, {"token": "tok-1"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DeviceToken.objects.exists())

    def test_unregistering_someone_elses_token_does_nothing(self):
        DeviceToken.objects.create(user=self.other_user, platform=DevicePlatform.ANDROID, token="tok-1")
        self.client.force_authenticate(self.user)
        response = self.client.post(UNREGISTER_URL, {"token": "tok-1"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(DeviceToken.objects.filter(token="tok-1", user=self.other_user).exists())

    def test_unregistering_an_unknown_token_is_a_no_op_not_an_error(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(UNREGISTER_URL, {"token": "never-registered"}, format="json")
        self.assertEqual(response.status_code, 200)
