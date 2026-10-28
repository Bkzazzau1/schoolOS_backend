"""The push-provider abstraction itself: honestly refuses when no real provider is
configured (this server genuinely has none until FIREBASE_* settings are set - no
vendor contract is faked here), and use_push_provider swaps it the same way
apps/administration/communication_delivery.py's own use_sms_provider/use_email_provider do.
"""

from django.test import SimpleTestCase

from apps.notifications.push_delivery import (
    DeliveryUnavailable,
    PushProvider,
    PushReceipt,
    UnconfiguredPushProvider,
    get_push_provider,
    use_push_provider,
)


class UnconfiguredProviderTests(SimpleTestCase):
    def test_honestly_refuses_rather_than_pretending_to_send(self):
        with self.assertRaises(DeliveryUnavailable) as caught:
            UnconfiguredPushProvider().send(tokens=["t1"], title="Hi", body="There", data={})
        self.assertEqual(caught.exception.code, "delivery_unavailable")


class ProviderRegistryTests(SimpleTestCase):
    def test_the_default_provider_is_the_honest_unconfigured_one(self):
        self.assertIsInstance(get_push_provider(), UnconfiguredPushProvider)

    def test_use_push_provider_overrides_get_push_provider_only_for_the_duration_of_the_block(self):
        class FakePush(PushProvider):
            def send(self, *, tokens, title, body, data):
                return [PushReceipt(token=t, sent=True) for t in tokens]

        fake = FakePush()
        with use_push_provider(fake):
            self.assertIs(get_push_provider(), fake)
            receipts = get_push_provider().send(tokens=["t1", "t2"], title="Hi", body="There", data={})
            self.assertEqual([r.sent for r in receipts], [True, True])
        self.assertIsInstance(get_push_provider(), UnconfiguredPushProvider)
