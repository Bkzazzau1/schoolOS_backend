"""The SMS/email delivery abstraction itself: honestly refuses when no real provider is configured (this
server genuinely has none - no vendor's API is invented here, see communication_delivery.py's own docstring),
and use_sms_provider/use_email_provider swap the same way apps/media/transcoding.py's own use_transcoder does.
"""

from django.test import SimpleTestCase

from apps.administration.communication_delivery import (
    DeliveryReceipt,
    DeliveryUnavailable,
    EmailProvider,
    UnconfiguredEmailProvider,
    UnconfiguredSmsProvider,
    get_email_provider,
    get_sms_provider,
    use_email_provider,
    use_sms_provider,
)


class UnconfiguredProviderTests(SimpleTestCase):
    def test_sms_honestly_refuses_rather_than_pretending_to_send(self):
        with self.assertRaises(DeliveryUnavailable) as caught:
            UnconfiguredSmsProvider().send(to="+2348030000001", body="Hello")
        self.assertEqual(caught.exception.code, "delivery_unavailable")

    def test_email_honestly_refuses_rather_than_pretending_to_send(self):
        with self.assertRaises(DeliveryUnavailable) as caught:
            UnconfiguredEmailProvider().send(to="guardian@example.com", subject="Notice", body="Hello")
        self.assertEqual(caught.exception.code, "delivery_unavailable")


class ProviderRegistryTests(SimpleTestCase):
    def test_the_default_sms_provider_is_the_honest_unconfigured_one(self):
        self.assertIsInstance(get_sms_provider(), UnconfiguredSmsProvider)

    def test_the_default_email_provider_is_the_honest_unconfigured_one(self):
        self.assertIsInstance(get_email_provider(), UnconfiguredEmailProvider)

    def test_use_sms_provider_overrides_get_sms_provider_only_for_the_duration_of_the_block(self):
        class FakeSms:
            def send(self, *, to, body):
                return DeliveryReceipt(provider_reference="fake-1", accepted_at="2026-01-01T00:00:00Z")

        fake = FakeSms()
        with use_sms_provider(fake):
            self.assertIs(get_sms_provider(), fake)
        self.assertIsInstance(get_sms_provider(), UnconfiguredSmsProvider)

    def test_use_email_provider_overrides_get_email_provider_only_for_the_duration_of_the_block(self):
        class FakeEmail(EmailProvider):
            def send(self, *, to, subject, body):
                return DeliveryReceipt(provider_reference="fake-1", accepted_at="2026-01-01T00:00:00Z")

        fake = FakeEmail()
        with use_email_provider(fake):
            self.assertIs(get_email_provider(), fake)
            receipt = get_email_provider().send(to="x@example.com", subject="s", body="b")
            self.assertEqual(receipt.provider_reference, "fake-1")
        self.assertIsInstance(get_email_provider(), UnconfiguredEmailProvider)
