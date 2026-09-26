"""A provider's callback: a public route that has to defend itself. Nothing is believed until the provider's own authenticity check has
passed (its signature, or - where it signs nothing - the provider's direct answer), the same delivery is never processed twice, one
school's delivery never reaches another, and nothing is ever attached to a family by guessing."""

import hashlib
import hmac
import json
from unittest import mock

from django.test import override_settings

from .. import sandbox_tools, webhooks
from ..models import BankAuditEvent, BankTransaction, BankWebhookEvent
from ..providers.monnify import clear_token_cache
from ..providers.paystack import SIGNATURE_HEADER as PAYSTACK_SIGNATURE
from ..providers.sandbox import SIGNATURE_HEADER
from ..providers.transport import use_transport
from .base import BankTestCase
from .fake_transport import FakeTransport, ok
from .test_monnify import CREDS as MONNIFY_CREDS
from .test_monnify import login, payment as monnify_payment
from .test_paystack import KEY as PAYSTACK_KEY
from .test_paystack import charge as paystack_charge


def header_name(name: str) -> str:
    return "HTTP_" + name.upper().replace("-", "_")


class WebhookTests(BankTestCase):
    def setUp(self):
        super().setUp()
        connection, path = self.connected()
        self.connection = self.row(connection)
        self.hook = path
        self.client.force_authenticate(None)  # a provider has no sign-in

    def url(self, hook=None):
        return f"/api/v1/{hook or self.hook}"

    def deliver(self, body, signature="valid", hook=None, **extra):
        headers = {}
        if signature == "valid":
            headers[header_name(SIGNATURE_HEADER)] = sandbox_tools.SandboxConnector.sign(body, self.secret_of(self.connection)["webhook_secret"])
        elif signature:
            headers[header_name(SIGNATURE_HEADER)] = signature
        return self.client.post(self.url(hook), data=body, content_type="application/json", **headers, **extra)

    def body(self, **over):
        body, _ = sandbox_tools.signed_webhook(self.connection, **over)
        return body

    # -- accepting ---------------------------------------------------------------------------

    def test_a_signed_delivery_becomes_a_transaction(self):
        response = self.deliver(self.body(external_transaction_id="W1", amount_minor=750_00, sender_name="Musa", receiving_account_reference="9000000001"))
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

    def test_a_signed_body_with_nothing_usable_is_acknowledged_and_stores_nothing(self):
        response = self.deliver(b"not json at all")
        self.assertEqual((response.status_code, response.json()["outcome"]), (200, "ignored"))
        self.assertEqual(BankTransaction.objects.count(), 0)

    def test_a_delivery_that_fails_half_way_is_not_remembered_as_done(self):
        body = self.body(external_transaction_id="W1")
        with mock.patch.object(webhooks, "ingest", side_effect=RuntimeError("database went away")):
            with self.assertRaises(RuntimeError):
                self.deliver(body)
        self.assertEqual((BankTransaction.objects.count(), BankWebhookEvent.objects.count()), (0, 0))
        self.assertEqual(self.deliver(body).json()["outcome"], "processed")
        self.assertEqual(BankTransaction.objects.count(), 1)

    def test_a_payment_into_an_account_no_family_has_is_kept_unmatched_and_never_guessed_at(self):
        # the narration and sender name say exactly who it is - and it is still not attached to anyone
        self.make_student("STU-0001", "Ahmad", "Bello", guardian="Musa Bello", phone="08031111111", admission="ADM/0001")
        self.deliver(self.body(
            external_transaction_id="W1", receiving_account_reference="9999999999", sender_name="Musa Bello", narration="Ahmad Bello STU-0001 ADM/0001 fees",
        ))
        row = BankTransaction.objects.get()
        self.assertEqual(row.reconciliation_status, "unmatched")
        self.assertIsNone(row.family_id)
        self.assertFalse(row.allocations.exists())

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
        self.assertEqual(self.row({"id": str(self.connection.id)}).webhook_status, "awaiting_event")

    def test_a_body_changed_after_signing_is_refused(self):
        body = self.body(external_transaction_id="W1", amount_minor=100)
        signature = sandbox_tools.SandboxConnector.sign(body, self.secret_of(self.connection)["webhook_secret"])
        self.assertEqual(self.deliver(body.replace(b"100", b"999"), signature=signature).status_code, 400)
        self.assertEqual(BankTransaction.objects.count(), 0)

    def test_an_unknown_address_is_a_plain_404_and_reveals_nothing(self):
        body = self.body()
        for hook in ("bank-webhooks/sandbox/not-a-real-token/", f"bank-webhooks/paystack/{self.hook.split('/')[2]}/", "bank-webhooks/gtbank/x/"):
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

    def test_a_disconnected_provider_no_longer_receives(self):
        body = self.body()
        self.client.force_authenticate(self.owner.user)
        self.client.post(self.path(f"connections/{self.connection.id}/disconnect/"))
        self.client.force_authenticate(None)
        self.assertEqual(self.deliver(body).status_code, 404)

    def test_a_disabled_provider_acknowledges_but_stores_nothing(self):
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

    def test_when_secure_storage_is_down_the_provider_is_told_to_retry_and_nothing_is_stored(self):
        body = self.body(external_transaction_id="W1")
        signature = sandbox_tools.SandboxConnector.sign(body, self.secret_of(self.connection)["webhook_secret"])
        with override_settings(BANKCONNECT_SECRET_KEYS=[]):
            response = self.deliver(body, signature=signature)
        self.assertEqual((response.status_code, response.json()["code"]), (503, "unavailable"))
        self.assertEqual(BankTransaction.objects.count(), 0)

    def test_it_needs_no_sign_in_and_reveals_no_secret(self):
        response = self.deliver(self.body(external_transaction_id="W1"))
        self.assertEqual(response.status_code, 200)
        self.assert_no_secrets(response.json(), response.content.decode())

    def test_the_stored_body_hash_is_all_that_is_kept_of_a_delivery(self):
        self.deliver(self.body(external_transaction_id="W1", sender_name="Musa Ibrahim"))
        event = BankWebhookEvent.objects.get()
        self.assertEqual(len(event.payload_hash), 64)
        self.assertNotIn("Musa", json.dumps({f.name: str(getattr(event, f.name)) for f in event._meta.fields}))

    # -- two schools -------------------------------------------------------------------------

    def test_a_delivery_reaches_only_its_own_school(self):
        other, other_hook = self.connected(who=self.other_owner, school=self.other_school)
        body, headers = sandbox_tools.signed_webhook(self.row(other), external_transaction_id="W1")
        response = self.client.post(self.url(other_hook), data=body, content_type="application/json", HTTP_X_SANDBOX_SIGNATURE=headers[SIGNATURE_HEADER])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(BankTransaction.objects.filter(school=self.school).count(), 0)
        self.assertEqual(BankTransaction.objects.get().school, self.other_school)

    def test_one_schools_signature_does_not_open_anothers_address(self):
        _, other_hook = self.connected(who=self.other_owner, school=self.other_school)
        body = self.body(external_transaction_id="W1")  # signed with OUR secret
        response = self.deliver(body, hook=other_hook)
        self.assertEqual((response.status_code, response.json()["code"]), (400, "invalid_signature"))
        self.assertEqual(BankTransaction.objects.count(), 0)


class RealProviderWebhookTests(BankTestCase):
    """The public route with the real providers' own authenticity rules (their documented servers stood in for by a fake)."""

    def setUp(self):
        super().setUp()
        clear_token_cache()
        self.addCleanup(clear_token_cache)
        self.server = FakeTransport()
        context = use_transport(self.server)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def connect_real(self, provider, credentials, environment="test", **kw):
        self.server.on("GET", "/dedicated_account/available_providers", ok({"status": True, "data": []}))
        self.server.on("POST", "/api/v1/auth/login", login())
        connection, hook = self.connected(provider=provider, credentials=credentials, environment=environment, **kw)
        self.client.force_authenticate(None)
        return self.row(connection), f"/api/v1/{hook}"

    # -- Paystack: signed with the school's own secret key --------------------------------------

    def test_a_paystack_delivery_signed_with_the_schools_key_is_accepted_and_a_forged_one_is_not(self):
        connection, url = self.connect_real("paystack", {"secret_key": PAYSTACK_KEY})
        raw = json.dumps(paystack_charge(receiver="9930000902", ident=91001)).encode()
        good = hmac.new(PAYSTACK_KEY.encode(), raw, hashlib.sha512).hexdigest()
        forged = self.client.post(url, data=raw, content_type="application/json", **{header_name(PAYSTACK_SIGNATURE): "0" * 128})
        self.assertEqual((forged.status_code, forged.json()["code"]), (400, "invalid_signature"))
        self.assertEqual(BankTransaction.objects.count(), 0)
        self.assertEqual(self.row({"id": str(connection.id)}).webhook_status, "awaiting_event")
        accepted = self.client.post(url, data=raw, content_type="application/json", **{header_name(PAYSTACK_SIGNATURE): good})
        self.assertEqual((accepted.status_code, accepted.json()["outcome"]), (200, "processed"))
        row = BankTransaction.objects.get()
        self.assertEqual((row.provider, row.external_transaction_id, row.amount_minor, row.receiving_account_ref), ("paystack", "91001", 15_000_000, "9930000902"))
        self.assertEqual(self.row({"id": str(connection.id)}).webhook_status, "active")  # only now
        again = self.client.post(url, data=raw, content_type="application/json", **{header_name(PAYSTACK_SIGNATURE): good})
        self.assertEqual(again.json()["outcome"], "duplicate")
        self.assertEqual(BankTransaction.objects.count(), 1)

    def test_a_paystack_payment_to_an_account_no_family_has_is_kept_unmatched(self):
        _, url = self.connect_real("paystack", {"secret_key": PAYSTACK_KEY})
        raw = json.dumps(paystack_charge(receiver="0000000000")).encode()
        sig = hmac.new(PAYSTACK_KEY.encode(), raw, hashlib.sha512).hexdigest()
        self.client.post(url, data=raw, content_type="application/json", **{header_name(PAYSTACK_SIGNATURE): sig})
        row = BankTransaction.objects.get()
        self.assertEqual((row.reconciliation_status, row.family_id), ("unmatched", None))

    def test_one_schools_paystack_key_does_not_open_anothers_address(self):
        self.connect_real("paystack", {"secret_key": PAYSTACK_KEY})
        other = "sk_test_ANOTHER-SCHOOLS-KEY"
        _, other_url = self.connect_real("paystack", {"secret_key": other}, who=self.other_owner, school=self.other_school)
        raw = json.dumps(paystack_charge()).encode()
        signed_with_ours = hmac.new(PAYSTACK_KEY.encode(), raw, hashlib.sha512).hexdigest()
        response = self.client.post(other_url, data=raw, content_type="application/json", **{header_name(PAYSTACK_SIGNATURE): signed_with_ours})
        self.assertEqual((response.status_code, response.json()["code"]), (400, "invalid_signature"))
        self.assertEqual(BankTransaction.objects.count(), 0)

    # -- Remita is not a Smart Money Collection provider ---------------------------------------

    def test_a_remita_address_is_not_a_route_even_if_an_old_row_still_carries_its_token(self):
        from .. import identifiers
        from ..constants import ConnectionStatus
        from ..models import CollectionProviderConnection

        CollectionProviderConnection.objects.create(
            school=self.school, provider="remita", status=ConnectionStatus.CONNECTED, webhook_token_hash=identifiers.hash_token("old-token"),
        )
        self.client.force_authenticate(None)
        for token in ("old-token", "no-such-token"):
            response = self.client.post(f"/api/v1/bank-webhooks/remita/{token}/", data="[]", content_type="application/json")
            self.assertEqual((response.status_code, response.json()["code"]), (404, "not_found"), token)
        self.assertEqual((BankTransaction.objects.count(), BankWebhookEvent.objects.count()), (0, 0))

    # -- Monnify: signed in production, confirmed by asking in the sandbox ----------------------

    def test_a_sandbox_monnify_delivery_is_confirmed_with_monnify_before_it_counts(self):
        _, url = self.connect_real("monnify", MONNIFY_CREDS)
        raw = json.dumps(monnify_payment(amount="9999999.00")).encode()  # unsigned, and claims far more than was paid
        self.server.on("GET", "/api/v2/merchant/transactions/query", ok({"requestSuccessful": True, "responseBody": {
            "transactionReference": "MNFY|20|0001", "paymentStatus": "PAID", "amountPaid": 1500.0, "currency": "NGN",
            "product": {"type": "RESERVED_ACCOUNT", "reference": "SOS-REF-1"}}}))
        response = self.client.post(url, data=raw, content_type="application/json")
        self.assertEqual((response.status_code, response.json()["outcome"]), (200, "processed"))
        self.assertEqual(BankTransaction.objects.get().amount_minor, 150_000)

    def test_a_live_monnify_delivery_without_a_signature_is_refused_without_asking_anyone(self):
        connection = self.connect_real("monnify", MONNIFY_CREDS)[0]
        connection.environment = "live"
        connection.save()
        url = f"/api/v1/bank-webhooks/monnify/{self.secret_of(connection)['webhook_token']}/"
        before = len(self.server.calls)
        response = self.client.post(url, data=json.dumps(monnify_payment()), content_type="application/json")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "invalid_signature"))
        self.assertEqual(len(self.server.calls), before)
        self.assertEqual(BankTransaction.objects.count(), 0)
