import json
from unittest import mock

from django.test import override_settings

from .. import sandbox_tools, webhooks
from ..models import BankAuditEvent, BankTransaction, BankWebhookEvent
from ..providers.sandbox import SIGNATURE_HEADER
from .base import BankTestCase


class WebhookTests(BankTestCase):
    def setUp(self):
        super().setUp()
        connection, path = self.connected()
        self.connection = self.row(connection)
        self.hook = path
        self.client.force_authenticate(None)  # a bank has no sign-in

    def url(self, hook=None):
        return f"/api/v1/{hook or self.hook}"

    def deliver(self, body, signature="valid", hook=None, **extra):
        headers = {}
        if signature == "valid":
            headers["HTTP_" + SIGNATURE_HEADER.upper().replace("-", "_")] = sandbox_tools.SandboxConnector.sign(
                body, self.secret_of(self.connection)["webhook_secret"]
            )
        elif signature:
            headers["HTTP_" + SIGNATURE_HEADER.upper().replace("-", "_")] = signature
        return self.client.post(self.url(hook), data=body, content_type="application/json", **headers, **extra)

    def body(self, **over):
        body, _ = sandbox_tools.signed_webhook(self.connection, **over)
        return body

    # -- accepting ---------------------------------------------------------------------------

    def test_a_signed_delivery_becomes_a_transaction(self):
        response = self.deliver(self.body(external_transaction_id="W1", amount_minor=750_00, sender_name="Musa", narration="BG-0042"))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"received": True, "outcome": "processed"})
        row = BankTransaction.objects.get()
        self.assertEqual((row.external_transaction_id, row.amount_minor, row.sender_name, row.connection), ("W1", 75_000, "Musa", self.connection))
        event = BankWebhookEvent.objects.get()
        self.assertTrue(event.signature_valid)
        self.assertEqual(event.outcome, "processed")
        self.assertIsNotNone(event.processed_at)

    def test_the_same_delivery_twice_is_processed_once(self):
        body = self.body(external_transaction_id="W1")
        self.assertEqual(self.deliver(body).json()["outcome"], "processed")
        self.assertEqual(self.deliver(body).json()["outcome"], "duplicate")
        self.assertEqual((BankTransaction.objects.count(), BankWebhookEvent.objects.count()), (1, 1))

    def test_a_redelivery_that_changed_in_transit_still_cannot_make_a_second_record(self):
        self.deliver(self.body(external_transaction_id="W1", narration="first"))
        response = self.deliver(self.body(external_transaction_id="W1", narration="second"))
        self.assertEqual(response.json()["outcome"], "processed")
        self.assertEqual(BankTransaction.objects.count(), 1)
        self.assertEqual(BankTransaction.objects.get().narration, "first")

    def test_a_signed_body_with_nothing_usable_is_accepted_and_stores_nothing(self):
        garbage = b"not json at all"
        response = self.deliver(garbage)
        self.assertEqual((response.status_code, response.json()["outcome"]), (200, "processed"))
        self.assertEqual(BankTransaction.objects.count(), 0)

    def test_a_delivery_that_fails_half_way_is_not_remembered_as_done(self):
        body = self.body(external_transaction_id="W1")
        with mock.patch.object(webhooks, "ingest", side_effect=RuntimeError("database went away")):
            with self.assertRaises(RuntimeError):
                self.deliver(body)
        self.assertEqual((BankTransaction.objects.count(), BankWebhookEvent.objects.count()), (0, 0))
        self.assertEqual(self.deliver(body).json()["outcome"], "processed")
        self.assertEqual(BankTransaction.objects.count(), 1)

    # -- refusing ----------------------------------------------------------------------------

    def test_a_bad_signature_is_refused_stored_nowhere_and_audited_without_secrets(self):
        body = self.body(external_transaction_id="W1")
        for signature in ("0" * 64, "", None):
            response = self.deliver(body, signature=signature)
            self.assertEqual((response.status_code, response.json()["code"]), (400, "invalid_signature"), signature)
        self.assertEqual((BankTransaction.objects.count(), BankWebhookEvent.objects.count()), (0, 0))
        event = BankAuditEvent.objects.filter(kind="webhook_rejected").first()
        self.assertEqual(event.detail, {"code": "invalid_signature"})
        self.assertEqual(event.connection, self.connection)

    def test_a_body_changed_after_signing_is_refused(self):
        body = self.body(external_transaction_id="W1", amount_minor=100)
        signature = sandbox_tools.SandboxConnector.sign(body, self.secret_of(self.connection)["webhook_secret"])
        tampered = body.replace(b"100", b"999")
        self.assertEqual(self.deliver(tampered, signature=signature).status_code, 400)
        self.assertEqual(BankTransaction.objects.count(), 0)

    def test_an_unknown_address_is_a_plain_404(self):
        body = self.body()
        for hook in ("bank-webhooks/sandbox/not-a-real-token/", f"bank-webhooks/gtbank/{self.hook.split('/')[2]}/"):
            response = self.deliver(body, hook=hook)
            self.assertEqual((response.status_code, response.json()), (404, {"code": "not_found"}), hook)
        self.assertEqual(BankAuditEvent.objects.filter(kind="webhook_rejected").count(), 0)

    def test_the_old_address_stops_working_when_a_new_one_is_issued(self):
        body = self.body(external_transaction_id="W1")
        self.client.force_authenticate(self.owner.user)
        new_hook = self.client.post(self.path(f"connections/{self.connection.id}/webhook-token/")).json()["webhook"]["path"]
        self.client.force_authenticate(None)
        self.assertEqual(self.deliver(body).status_code, 404)
        self.assertEqual(self.deliver(body, hook=new_hook).status_code, 200)

    def test_a_disconnected_account_no_longer_receives(self):
        body = self.body()
        self.client.force_authenticate(self.owner.user)
        self.client.post(self.path(f"connections/{self.connection.id}/disconnect/"))
        self.client.force_authenticate(None)
        self.assertEqual(self.deliver(body).status_code, 404)

    def test_a_disabled_account_acknowledges_but_stores_nothing(self):
        body = self.body(external_transaction_id="W1")
        self.client.force_authenticate(self.owner.user)
        self.client.post(self.path(f"connections/{self.connection.id}/disable/"))
        self.client.force_authenticate(None)
        response = self.deliver(body)
        self.assertEqual((response.status_code, response.json()["outcome"]), (200, "ignored"))
        self.assertEqual((BankTransaction.objects.count(), BankWebhookEvent.objects.count()), (0, 0))

    def test_an_oversized_body_is_refused_before_it_is_read(self):
        response = self.deliver(b"x" * (webhooks.MAX_BODY_BYTES + 1), signature=None)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(BankWebhookEvent.objects.count(), 0)

    def test_when_secure_storage_is_down_the_provider_is_told_to_retry(self):
        body = self.body(external_transaction_id="W1")
        signature = sandbox_tools.SandboxConnector.sign(body, self.secret_of(self.connection)["webhook_secret"])
        with override_settings(BANKCONNECT_SECRET_KEYS=[]):
            response = self.deliver(body, signature=signature)
        self.assertEqual((response.status_code, response.json()["code"]), (503, "unavailable"))
        self.assertEqual(BankTransaction.objects.count(), 0)

    def test_it_needs_no_sign_in_and_reveals_nothing(self):
        response = self.deliver(self.body(external_transaction_id="W1"))
        self.assertEqual(response.status_code, 200)
        self.assert_no_secrets(response.json(), response.content.decode())

    # -- two schools -------------------------------------------------------------------------

    def test_a_delivery_reaches_only_its_own_school(self):
        other, other_hook = self.connected(who=self.other_owner, school=self.other_school)
        other_row = self.row(other)
        body, headers = sandbox_tools.signed_webhook(other_row, external_transaction_id="W1")
        response = self.client.post(
            self.url(other_hook), data=body, content_type="application/json",
            HTTP_X_SANDBOX_SIGNATURE=headers[SIGNATURE_HEADER],
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(BankTransaction.objects.filter(school=self.school).count(), 0)
        self.assertEqual(BankTransaction.objects.get().school, self.other_school)

    def test_one_schools_signature_does_not_open_anothers_address(self):
        other, other_hook = self.connected(who=self.other_owner, school=self.other_school, account="0123456780")
        body = self.body(external_transaction_id="W1")  # signed with OUR secret
        response = self.deliver(body, hook=other_hook)
        self.assertEqual((response.status_code, response.json()["code"]), (400, "invalid_signature"))
        self.assertEqual(BankTransaction.objects.count(), 0)

    def test_the_stored_body_hash_is_all_that_is_kept_of_a_delivery(self):
        self.deliver(self.body(external_transaction_id="W1", sender_name="Musa Ibrahim"))
        event = BankWebhookEvent.objects.get()
        self.assertEqual(len(event.payload_hash), 64)
        self.assertNotIn("Musa", json.dumps({f.name: str(getattr(event, f.name)) for f in event._meta.fields}))
