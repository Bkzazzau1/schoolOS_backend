"""The Paystack connector, against Paystack's own documentation: what it sends (path, bearer key, body) and how it reads the answers, the
webhook's signature, and what it does when Paystack does not answer."""

import hashlib
import hmac
import json

from django.test import SimpleTestCase

from ..providers import base
from ..providers.base import CustomerDetails, ProvisionRequest
from ..providers.paystack import SIGNATURE_HEADER, PaystackConnector, key_environment
from ..providers.transport import use_transport
from .fake_transport import FakeTransport, ok

KEY = "sk_test_UNIQUE-PAYSTACK-KEY-1"
LIVE_KEY = "sk_live_UNIQUE-PAYSTACK-KEY-2"
SECRET = {"secret_key": KEY, "connection_id": "c-1"}


def customer_created(code="CUS_abc123", cid=297346561):
    return ok({"status": True, "message": "Customer created", "data": {"email": "musa@example.com", "customer_code": code, "id": cid}})


def dva(number="9930000902", ident=173, assigned=True, active=True):
    return {
        "status": True, "message": "Dedicated account created",
        "data": {
            "bank": {"name": "Wema Bank", "id": 20, "slug": "wema-bank"}, "account_name": "KAROKART/MUSA BELLO", "account_number": number,
            "assigned": assigned, "currency": "NGN", "active": active, "id": ident,
        },
    }


def request(**over):
    fields = dict(
        idempotency_key="k-1", account_reference="SOS-REF-1", family_code="FAM-K7Q2M9XA", family_name="Bello family",
        customer=CustomerDetails(name="Musa Bello", first_name="Musa", last_name="Bello", email="musa@example.com", phone="08031111111"),
        account_name="Bello family",
    )
    fields.update(over)
    return ProvisionRequest(**fields)


def signed(body: dict, key=KEY):
    raw = json.dumps(body).encode()
    return raw, {SIGNATURE_HEADER: hmac.new(key.encode(), raw, hashlib.sha512).hexdigest()}


def charge(receiver="9930000902", channel="dedicated_nuban", amount=15_000_000, ident=91001):
    return {
        "event": "charge.success",
        "data": {
            "id": ident, "reference": "REF-9", "amount": amount, "currency": "NGN", "status": "success", "paid_at": "2026-09-25T10:30:00.000Z",
            "channel": channel,
            "authorization": {
                "channel": channel, "sender_bank_account_number": "XXXXXX0011", "sender_name": "RANDALL AKANBI", "sender_bank": "First City Monument Bank",
                "narration": "NIP: fees", "receiver_bank_account_number": receiver,
            },
        },
    }


class PaystackConnectTests(SimpleTestCase):
    def setUp(self):
        self.paystack = PaystackConnector()
        self.server = FakeTransport().on("GET", "/dedicated_account/available_providers", ok({"status": True, "data": []}))

    def validate(self, key=KEY, environment="test", settings=None):
        with use_transport(self.server):
            return self.paystack.validate_credentials(environment=environment, credentials={"secret_key": key}, settings=settings or {})

    def test_the_key_is_proved_with_a_bearer_call_to_paystack_and_the_test_bank_is_the_default(self):
        result = self.validate()
        (call,) = self.server.calls
        self.assertEqual((call.method, call.path, call.host), ("GET", "/dedicated_account/available_providers", "api.paystack.co"))
        self.assertEqual(call.headers["Authorization"], f"Bearer {KEY}")
        self.assertEqual(result.settings, {"preferred_bank": "test-bank"})
        self.assertEqual(result.secret, {"secret_key": KEY})

    def test_a_live_key_defaults_to_a_documented_live_bank_and_only_those_are_allowed(self):
        self.assertEqual(self.validate(LIVE_KEY, "live").settings, {"preferred_bank": "wema-bank"})
        self.assertEqual(self.validate(LIVE_KEY, "live", {"preferred_bank": "titan-paystack"}).settings, {"preferred_bank": "titan-paystack"})
        with self.assertRaises(base.ProviderRejected) as caught:
            self.validate(LIVE_KEY, "live", {"preferred_bank": "test-bank"})
        self.assertEqual(caught.exception.code, "invalid_setting")

    def test_a_test_key_cannot_be_connected_as_live_and_the_reverse(self):
        for key, environment in ((KEY, "live"), (LIVE_KEY, "test")):
            with self.assertRaises(base.ProviderRejected) as caught:
                self.validate(key, environment)
            self.assertEqual(caught.exception.code, "environment_mismatch")
        self.assertEqual(self.server.calls, [])  # refused before the key was sent anywhere

    def test_something_that_is_not_a_paystack_key_is_refused_without_calling_paystack(self):
        with self.assertRaises(base.ProviderRejected):
            self.validate("not-a-key")
        self.assertEqual(self.server.calls, [])

    def test_a_key_paystack_refuses_is_bad_credentials(self):
        self.server.on("GET", "/dedicated_account/available_providers", ok({"status": False, "message": "Invalid key"}, 401))
        with self.assertRaises(base.BadCredentials):
            self.validate()

    def test_key_environment_reads_the_documented_prefix(self):
        self.assertEqual((key_environment("sk_live_x"), key_environment("sk_test_x"), key_environment("pk_live_x")), ("live", "test", None))


class PaystackProvisionTests(SimpleTestCase):
    def setUp(self):
        self.paystack = PaystackConnector()
        self.server = FakeTransport()
        self.server.on("POST", "/customer", customer_created()).on("POST", "/dedicated_account", ok(dva()))

    def provision(self, req=None, environment="test", settings=None):
        with use_transport(self.server):
            return self.paystack.provision_family_collection_account(
                SECRET, req or request(), environment=environment, settings=settings or {"preferred_bank": "test-bank"}
            )

    def test_a_customer_is_made_then_a_dedicated_account_for_them(self):
        account = self.provision()
        customer, dedicated = self.server.to("POST", "/customer")[0], self.server.to("POST", "/dedicated_account")[0]
        self.assertEqual(customer.headers["Authorization"], f"Bearer {KEY}")
        self.assertEqual(
            {k: customer.json[k] for k in ("email", "first_name", "last_name", "phone")},
            {"email": "musa@example.com", "first_name": "Musa", "last_name": "Bello", "phone": "08031111111"},
        )
        meta = json.loads(customer.json["metadata"])
        self.assertEqual(meta["schoolos_family"], "FAM-K7Q2M9XA")
        self.assertEqual(dedicated.json, {"customer": "CUS_abc123", "preferred_bank": "test-bank"})
        self.assertEqual(
            (account.account_number, account.provider_account_ref, account.lookup_ref, account.bank_name, account.account_name, account.ready),
            ("9930000902", "173", "9930000902", "Wema Bank", "KAROKART/MUSA BELLO", True),
        )

    def test_the_progress_made_is_kept_so_a_retry_does_not_make_a_second_customer(self):
        req = request()
        self.server.on("POST", "/dedicated_account", [base.ProviderUnavailable(), ok(dva())])
        self.server.on("GET", "/dedicated_account", ok({"status": True, "data": []}))
        with self.assertRaises(base.ProviderUnavailable):
            self.provision(req)
        self.assertEqual(req.checkpoint["customer_code"], "CUS_abc123")
        self.assertEqual(self.provision(req).account_number, "9930000902")
        self.assertEqual(self.server.called("POST", "/customer"), 1)  # made once, however many attempts

    def test_after_a_timeout_the_account_that_may_already_exist_is_found_not_made_again(self):
        self.server.on("POST", "/dedicated_account", base.ProviderUnavailable())
        self.server.on("GET", "/dedicated_account", ok({"status": True, "data": [dva(number="9930000777", ident=555)["data"]]}))
        account = self.provision()
        self.assertEqual((account.account_number, account.provider_account_ref), ("9930000777", "555"))
        listing = self.server.to("GET", "/dedicated_account")[0]
        self.assertEqual(listing.query["customer"], "297346561")

    def test_after_a_timeout_when_nothing_exists_the_failure_is_reported_as_unknown_not_as_done(self):
        self.server.on("POST", "/dedicated_account", base.ProviderUnavailable())
        self.server.on("GET", "/dedicated_account", ok({"status": True, "data": []}))
        with self.assertRaises(base.ProviderUnavailable):
            self.provision()

    def test_a_customer_whose_creation_timed_out_is_found_by_email(self):
        self.server.on("POST", "/customer", base.ProviderUnavailable())
        self.server.on("GET", "/customer", ok({"status": True, "data": [{"email": "MUSA@example.com", "customer_code": "CUS_found", "id": 42}]}))
        self.provision()
        self.assertEqual(self.server.to("POST", "/dedicated_account")[0].json["customer"], "CUS_found")

    def test_an_account_paystack_is_still_assigning_is_not_yet_ready(self):
        self.server.on("POST", "/dedicated_account", ok(dva(assigned=False)))
        self.assertFalse(self.provision().ready)

    def test_a_payer_without_an_email_is_refused_before_anything_is_sent(self):
        with self.assertRaises(base.ProviderRejected) as caught:
            self.provision(request(customer=CustomerDetails(name="Musa", phone="0803")))
        self.assertEqual(caught.exception.code, "customer_details_missing")
        self.assertEqual(self.server.calls, [])

    def test_a_refusal_carries_schoolos_words_never_paystacks(self):
        self.server.on("POST", "/dedicated_account", ok({"status": False, "message": "Customer SECRET-DETAIL cannot have one"}, 400))
        with self.assertRaises(base.ProviderRejected) as caught:
            self.provision()
        self.assertNotIn("SECRET-DETAIL", caught.exception.message)
        self.assertNotIn(KEY, caught.exception.message)

    def test_a_server_error_is_an_unknown_outcome(self):
        self.server.on("POST", "/customer", ok({"message": "boom"}, 502)).on("GET", "/customer", ok({"status": True, "data": []}))
        with self.assertRaises(base.ProviderUnavailable):
            self.provision()


class PaystackLifecycleTests(SimpleTestCase):
    def setUp(self):
        self.paystack = PaystackConnector()
        self.server = FakeTransport()

    def run_with(self, name, **kw):
        with use_transport(self.server):
            return getattr(self.paystack, name)(SECRET, environment="test", settings={}, **kw)

    def test_an_account_is_read_by_its_id(self):
        self.server.on("GET", "/dedicated_account/173", ok(dva(active=True, assigned=True)))
        self.assertEqual(self.run_with("get_collection_account", account_ref="173").status, "active")
        self.server.on("GET", "/dedicated_account/173", ok(dva(assigned=False)))
        self.assertEqual(self.run_with("get_collection_account", account_ref="173").status, "pending")
        self.server.on("GET", "/dedicated_account/173", ok(dva(active=False)))
        self.assertEqual(self.run_with("get_collection_account", account_ref="173").status, "inactive")

    def test_retiring_an_account_deactivates_it_and_paystack_documents_no_way_back(self):
        self.server.on("DELETE", "/dedicated_account/173", ok(dva(active=False)))
        self.run_with("deactivate_collection_account", account_ref="173")
        self.run_with("close_collection_account", account_ref="173")
        self.assertEqual(self.server.called("DELETE", "/dedicated_account/173"), 2)
        with self.assertRaises(base.NotSupported):
            self.run_with("reactivate_collection_account", account_ref="173")
        self.assertFalse(self.paystack.info.capabilities.supports_account_reactivation)

    def test_a_payment_is_confirmed_by_asking_paystack(self):
        self.server.on("GET", "/transaction/verify/REF-9", ok({"status": True, "data": {
            "status": "success", "reference": "REF-9", "amount": 15_000_000, "currency": "NGN",
            "authorization": {"receiver_bank_account_number": "9930000902"}}}))
        result = self.run_with("verify_transaction", reference="REF-9")
        self.assertEqual((result.found, result.paid, result.amount_minor, result.receiving_reference), (True, True, 15_000_000, "9930000902"))
        self.server.on("GET", "/transaction/verify/NOPE", ok({"status": False, "message": "Transaction reference not found"}, 404))
        self.assertFalse(self.run_with("verify_transaction", reference="NOPE").found)


class PaystackWebhookTests(SimpleTestCase):
    def setUp(self):
        self.paystack = PaystackConnector()

    def receive(self, raw, headers, key=KEY):
        return self.paystack.handle_webhook(raw_body=raw, headers=headers, secret={"secret_key": key}, environment="test", settings={})

    def test_a_signed_transfer_into_a_family_account_becomes_a_normalised_payment(self):
        outcome = self.receive(*signed(charge()))
        (tx,) = outcome.transactions
        self.assertEqual(
            (tx.external_transaction_id, tx.direction, tx.amount_minor, tx.currency, tx.receiving_account_reference),
            ("91001", "credit", 15_000_000, "NGN", "9930000902"),
        )
        self.assertEqual((tx.sender_name, tx.sender_bank, tx.narration, tx.transaction_type), ("RANDALL AKANBI", "First City Monument Bank", "NIP: fees", "dedicated_nuban"))

    def test_a_forged_or_unsigned_delivery_is_refused_and_nothing_in_it_is_read(self):
        raw, headers = signed(charge())
        for bad in ({}, {SIGNATURE_HEADER: "0" * 128}, {SIGNATURE_HEADER: headers[SIGNATURE_HEADER][:-1] + "0"}):
            with self.assertRaises(base.InvalidSignature):
                self.receive(raw, bad)

    def test_a_body_changed_after_signing_is_refused(self):
        raw, headers = signed(charge(amount=100))
        with self.assertRaises(base.InvalidSignature):
            self.receive(raw.replace(b"100", b"999"), headers)

    def test_a_delivery_signed_with_another_key_is_refused(self):
        with self.assertRaises(base.InvalidSignature):
            self.receive(*signed(charge(), key=LIVE_KEY))

    def test_a_card_charge_is_not_a_payment_into_a_family_account(self):
        outcome = self.receive(*signed(charge(channel="card")))
        self.assertTrue(outcome.ignored and not outcome.transactions)

    def test_the_account_being_ready_or_failing_is_reported(self):
        ready = self.receive(*signed({"event": "dedicatedaccount.assign.success", "data": {"customer": {"customer_code": "CUS_abc"}, "dedicated_account": {"account_number": "9930000902"}}}))
        failed = self.receive(*signed({"event": "dedicatedaccount.assign.failed", "data": {"customer": {"customer_code": "CUS_abc"}}}))
        self.assertEqual([(e.kind, e.reference, e.account_number) for e in ready.account_events], [("ready", "CUS_abc", "9930000902")])
        self.assertEqual([e.kind for e in failed.account_events], ["failed"])

    def test_an_event_that_means_nothing_here_is_acknowledged_and_ignored(self):
        self.assertTrue(self.receive(*signed({"event": "subscription.create", "data": {}})).ignored)

    def test_an_unreadable_body_is_refused_after_the_signature_passes(self):
        raw = b"not json"
        with self.assertRaises(base.ConnectorError):
            self.receive(raw, {SIGNATURE_HEADER: hmac.new(KEY.encode(), raw, hashlib.sha512).hexdigest()})


class PaystackCapabilityTests(SimpleTestCase):
    def test_what_paystack_can_and_cannot_do_is_stated(self):
        c = PaystackConnector.info.capabilities
        self.assertTrue(c.supports_family_collection_accounts and c.supports_static_accounts and c.supports_webhooks and c.supports_transaction_requery)
        self.assertTrue(c.supports_account_deactivation and c.supports_account_closure)
        self.assertFalse(c.supports_account_reactivation or c.requires_customer_kyc)
        self.assertEqual(PaystackConnector.info.webhook.mode, "dashboard")  # documented as set in the dashboard, not by API
