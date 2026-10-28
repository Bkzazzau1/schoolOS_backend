"""notify()/notify_many() push to every device registered to the recipient's account,
best effort: a missing token, an unconfigured provider, or a genuine provider failure
never stops (or rolls back) the in-app Notification these always create first.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import DeviceToken, DevicePlatform
from apps.notifications.models import Notification
from apps.notifications.push_delivery import DeliveryUnavailable, PushProvider, PushReceipt, use_push_provider
from apps.notifications.services import notify, notify_many
from apps.schools.models import Membership, Role, School

User = get_user_model()


class RecordingPushProvider(PushProvider):
    def __init__(self):
        self.calls = []

    def send(self, *, tokens, title, body, data):
        self.calls.append({"tokens": sorted(tokens), "title": title, "body": body, "data": data})
        return [PushReceipt(token=t, sent=True) for t in tokens]


class FailingPushProvider(PushProvider):
    def send(self, *, tokens, title, body, data):
        raise DeliveryUnavailable()


class PushIntegrationTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="A", slug="a")
        self.user = User.objects.create_user("ada@a.ng", "a-long-test-password-1")
        self.ada = Membership.objects.create(user=self.user, school=self.school, role=Role.TEACHER)
        self.bola_user = User.objects.create_user("bola@a.ng", "a-long-test-password-1")
        self.bola = Membership.objects.create(user=self.bola_user, school=self.school, role=Role.PARENT)

    def test_notify_pushes_to_every_device_registered_to_the_recipients_account(self):
        DeviceToken.objects.create(user=self.user, platform=DevicePlatform.ANDROID, token="tok-android")
        DeviceToken.objects.create(user=self.user, platform=DevicePlatform.IOS, token="tok-ios")
        provider = RecordingPushProvider()
        with use_push_provider(provider):
            notify(self.ada, "x", "Hello", "World")
        self.assertEqual(len(provider.calls), 1)
        call = provider.calls[0]
        self.assertEqual(call["tokens"], ["tok-android", "tok-ios"])
        self.assertEqual((call["title"], call["body"]), ("Hello", "World"))
        self.assertEqual(call["data"]["kind"], "x")

    def test_notify_does_nothing_when_the_account_has_no_registered_device(self):
        provider = RecordingPushProvider()
        with use_push_provider(provider):
            notify(self.ada, "x", "Hello", "World")
        self.assertEqual(provider.calls, [])

    def test_an_unconfigured_or_failing_provider_never_blocks_the_in_app_notification(self):
        DeviceToken.objects.create(user=self.user, platform=DevicePlatform.ANDROID, token="tok-1")
        with use_push_provider(FailingPushProvider()):
            notification = notify(self.ada, "x", "Hello", "World")
        self.assertIsNotNone(notification.id)
        self.assertEqual(Notification.objects.count(), 1)

    def test_notify_many_pushes_to_each_recipients_own_devices_only(self):
        DeviceToken.objects.create(user=self.user, platform=DevicePlatform.ANDROID, token="tok-ada")
        DeviceToken.objects.create(user=self.bola_user, platform=DevicePlatform.ANDROID, token="tok-bola")
        provider = RecordingPushProvider()
        with use_push_provider(provider):
            told = notify_many([self.ada, self.bola], "x", "Hello", "World")
        self.assertEqual(told, 2)
        self.assertEqual(sorted(c["tokens"][0] for c in provider.calls), ["tok-ada", "tok-bola"])

    def test_notify_many_with_no_recipients_does_not_call_the_provider(self):
        provider = RecordingPushProvider()
        with use_push_provider(provider):
            self.assertEqual(notify_many([], "x", "t", "m"), 0)
        self.assertEqual(provider.calls, [])

    def test_a_device_registered_to_someone_else_never_receives_another_persons_push(self):
        DeviceToken.objects.create(user=self.bola_user, platform=DevicePlatform.ANDROID, token="tok-bola")
        provider = RecordingPushProvider()
        with use_push_provider(provider):
            notify(self.ada, "x", "Private", "for ada only")
        self.assertEqual(provider.calls, [])
