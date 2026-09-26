"""The Remita connector, against Remita's own documentation. Remita has no virtual bank accounts: a family's payment reference is an
invoice (an RRR), Remita's notification is not signed so every one is confirmed by asking Remita, and its live address is not in its
documentation so a live connection is refused until an operator configures it."""

import json
from datetime import date

from django.test import SimpleTestCase, override_settings

from ..providers import base
from ..providers.base import CustomerDetails, ProvisionRequest
from ..providers.remita import DEMO_BASE, DUMMY_RRR, RemitaConnector, sha512
from ..providers.transport import HttpResult, use_transport
from .fake_transport import FakeTransport, ok

MERCHANT, API_KEY, SERVICE = "2547916", "UNIQUE-REMITA-API-KEY", "4430731"
CREDS = {"merchant_id": MERCHANT, "api_key": API_KEY, "service_type_id": SERVICE}
ROOT = "/remita/exapp/api/v1/send/api"
INIT = f"{ROOT}/echannelsvc/merchant/api/paymentinit"
CANCEL = f"{ROOT}/echannelsvc/v2/api/deactivate.json"


def status_path(rrr):
    return f"{ROOT}/echannelsvc/{MERCHANT}/{rrr}/{sha512(rrr, API_KEY, MERCHANT)}/status.reg"


def order_path(order_id):
    return f"{ROOT}/echannelsvc/{MERCHANT}/{order_id}/{sha512(order_id, API_KEY, MERCHANT)}/orderstatus.reg"


def jsonp(body):
    return HttpResult(200, None, f"jsonp ({json.dumps(body)})")


def generated(rrr="280007512345"):
    return jsonp({"statuscode": "025", "RRR": rrr, "status": "Payment Reference generated"})


def paid(rrr="280007512345", amount=150000):
    return ok({"RRR": rrr, "status": "01", "amount": amount, "message": "Successful", "orderId": "SOS-REF-1"})


def request(**over):
    fields = dict(
        idempotency_key="k-1", account_reference="SOS-REF-1", family_code="FAM-K7Q2M9XA", family_name="Bello family",
        customer=CustomerDetails(name="Musa Bello", email="musa@example.com", phone="08031111111"), account_name="Bello family",
        mode="dynamic", amount_minor=15_000_000,
    )
    fields.update(over)
    return ProvisionRequest(**fields)


class RemitaConnectTests(SimpleTestCase):
    def setUp(self):
        self.remita = RemitaConnector()
        self.server = FakeTransport().on("GET", status_path(DUMMY_RRR), ok({"status": "021", "message": "Transaction Not Found"}))

    def validate(self, credentials=CREDS, environment="test"):
        with use_transport(self.server):
            return self.remita.validate_credentials(environment=environment, credentials=credentials, settings={})

    def test_the_keys_are_proved_with_a_status_check_for_a_reference_that_cannot_exist(self):
        result = self.validate()
        (call,) = self.server.calls
        self.assertEqual(call.host, "demo.remita.net")
        token = sha512(DUMMY_RRR, API_KEY, MERCHANT)
        self.assertEqual(call.headers["Authorization"], f"remitaConsumerKey={MERCHANT},remitaConsumerToken={token}")
        self.assertEqual((result.secret, result.merchant.reference), (CREDS, "****7916"))
        self.assertFalse(result.meta["service_type_verified"])

    def test_wrong_keys_are_answered_by_remita_with_an_authentication_code(self):
        for code in ("013", "020", "033"):
            self.server.on("GET", status_path(DUMMY_RRR), ok({"status": code}))
            with self.assertRaises(base.BadCredentials):
                self.validate()

    def test_a_missing_credential_is_refused_before_anything_is_sent(self):
        with self.assertRaises(base.BadCredentials):
            self.validate({"merchant_id": MERCHANT, "api_key": "", "service_type_id": SERVICE})
        self.assertEqual(self.server.calls, [])

    def test_a_live_connection_is_refused_until_the_operator_has_configured_remitas_live_address(self):
        with self.assertRaises(base.ProviderRejected) as caught:
            self.validate(environment="live")
        self.assertEqual(caught.exception.code, "live_not_configured")
        self.assertEqual(self.server.calls, [])

    @override_settings(COLLECTION_REMITA_LIVE_BASE_URL="https://live.example-remita.test/api/")
    def test_once_configured_live_goes_to_the_configured_host(self):
        self.server.on("GET", f"/api/echannelsvc/{MERCHANT}/{DUMMY_RRR}/{sha512(DUMMY_RRR, API_KEY, MERCHANT)}/status.reg", ok({"status": "021"}))
        self.validate(environment="live")
        self.assertEqual(self.server.calls[0].host, "live.example-remita.test")

    def test_demo_address_is_the_documented_one(self):
        self.assertEqual(DEMO_BASE, "https://demo.remita.net/remita/exapp/api/v1/send/api")


class RemitaProvisionTests(SimpleTestCase):
    def setUp(self):
        self.remita = RemitaConnector()
        self.server = FakeTransport().on("POST", INIT, generated())

    def provision(self, req=None):
        with use_transport(self.server):
            return self.remita.provision_family_collection_account(CREDS, req or request(), environment="test", settings={})

    def test_an_invoice_is_created_with_the_documented_hash_and_the_rrr_is_the_family_account(self):
        account = self.provision(request(valid_until=date(2026, 12, 31)))
        (call,) = self.server.to("POST", INIT)
        token = sha512(MERCHANT, SERVICE, "SOS-REF-1", "150000", API_KEY)
        self.assertEqual(call.headers["Authorization"], f"remitaConsumerKey={MERCHANT},remitaConsumerToken={token}")
        self.assertEqual(
            call.json,
            {
                "serviceTypeId": SERVICE, "amount": "150000", "orderId": "SOS-REF-1", "payerName": "Musa Bello", "payerEmail": "musa@example.com",
                "payerPhone": "08031111111", "description": "School fees - Bello family", "expiryDate": "31/12/2026",
            },
        )
        self.assertEqual(
            (account.account_number, account.provider_account_ref, account.lookup_ref, account.number_label, account.bank_name),
            ("280007512345", "280007512345", "280007512345", "Remita Retrieval Reference (RRR)", "Remita"),
        )

    def test_kobo_amounts_are_written_as_naira_with_two_places_only_when_needed(self):
        self.provision(request(amount_minor=15_050))
        self.assertEqual(self.server.calls[0].json["amount"], "150.50")

    def test_a_collection_target_is_required_because_an_invoice_is_made_for_an_amount(self):
        for amount in (None, 0):
            with self.assertRaises(base.ProviderRejected) as caught:
                self.provision(request(amount_minor=amount))
            self.assertEqual(caught.exception.code, "amount_required")
        self.assertEqual(self.server.calls, [])

    def test_the_payers_name_email_and_phone_are_all_needed(self):
        with self.assertRaises(base.ProviderRejected) as caught:
            self.provision(request(customer=CustomerDetails(name="Musa", email="musa@example.com")))
        self.assertEqual(caught.exception.code, "customer_details_missing")

    def test_after_a_timeout_the_rrr_that_may_exist_is_found_by_order_id_not_made_again(self):
        self.server.on("POST", INIT, base.ProviderUnavailable())
        self.server.on("GET", order_path("SOS-REF-1"), ok({"RRR": "280007599999", "status": "021"}))
        self.assertEqual(self.provision().account_number, "280007599999")
        self.assertEqual(self.server.called("POST", INIT), 1)

    def test_a_timeout_with_nothing_found_stays_unknown(self):
        self.server.on("POST", INIT, base.ProviderUnavailable())
        self.server.on("GET", order_path("SOS-REF-1"), ok({"status": "021", "message": "not found"}))
        with self.assertRaises(base.ProviderUnavailable):
            self.provision()

    def test_a_duplicate_order_id_leads_to_the_existing_rrr(self):
        self.server.on("POST", INIT, jsonp({"statuscode": "028", "status": "Duplicate"}))
        self.server.on("GET", order_path("SOS-REF-1"), ok({"RRR": "280007588888", "status": "021"}))
        self.assertEqual(self.provision().account_number, "280007588888")

    def test_a_refusal_is_reported_in_schoolos_words(self):
        self.server.on("POST", INIT, jsonp({"statuscode": "999", "status": f"bad service type {SERVICE} {API_KEY}"}))
        with self.assertRaises(base.ProviderRejected) as caught:
            self.provision()
        for leaked in (SERVICE, API_KEY):
            self.assertNotIn(leaked, caught.exception.message)

    def test_an_authentication_answer_is_bad_credentials(self):
        self.server.on("POST", INIT, jsonp({"statuscode": "020", "status": "unauthorised"}))
        with self.assertRaises(base.BadCredentials):
            self.provision()


class RemitaLifecycleTests(SimpleTestCase):
    def setUp(self):
        self.remita = RemitaConnector()
        self.server = FakeTransport()

    def run_with(self, name, **kw):
        with use_transport(self.server):
            return getattr(self.remita, name)(CREDS, environment="test", settings={}, **kw)

    def test_a_reference_is_cancelled_with_the_documented_hash_on_the_cancel_host(self):
        self.server.on("POST", CANCEL, jsonp({"statuscode": "00", "status": "Successful"}))
        self.run_with("close_collection_account", account_ref="280007512345")
        (call,) = self.server.calls
        self.assertEqual(call.host, "remitademo.net")
        self.assertEqual(call.json, {"rrr": "280007512345", "merchantId": MERCHANT, "hash": sha512("280007512345", API_KEY, MERCHANT)})

    def test_a_reference_that_cannot_be_cancelled_is_refused_not_assumed_closed(self):
        self.server.on("POST", CANCEL, jsonp({"statuscode": "34", "status": "Already paid"}))
        with self.assertRaises(base.ProviderRejected):
            self.run_with("close_collection_account", account_ref="280007512345")

    def test_only_closing_is_offered_and_no_static_account_exists(self):
        for name in ("deactivate_collection_account", "reactivate_collection_account"):
            with self.assertRaises(base.NotSupported):
                self.run_with(name, account_ref="280007512345")
        c = RemitaConnector.info.capabilities
        self.assertTrue(c.supports_dynamic_accounts and c.supports_account_closure and c.supports_transaction_requery)
        self.assertFalse(c.supports_static_accounts or c.requires_customer_kyc)
        self.assertTrue(c.supports_direct_debit_mandates)  # a capability, not part of the family accounts flow

    def test_a_paid_reference_reads_as_settled_and_an_open_one_as_active(self):
        self.server.on("GET", status_path("R1"), paid("R1"))
        self.assertEqual(self.run_with("get_collection_account", account_ref="R1").status, "closed")
        self.server.on("GET", status_path("R2"), ok({"RRR": "R2", "status": "021"}))
        self.assertEqual(self.run_with("get_collection_account", account_ref="R2").status, "active")

    def test_a_payment_is_confirmed_by_asking_remita_including_the_jsonp_form(self):
        self.server.on("GET", status_path("R1"), paid("R1", 1500))
        result = self.run_with("verify_transaction", reference="R1")
        self.assertEqual((result.found, result.paid, result.amount_minor, result.receiving_reference), (True, True, 150_000, "R1"))
        self.server.on("GET", status_path("R3"), HttpResult(200, None, 'jsonp ({"RRR": "R3", "status": "00", "amount": 20})'))
        self.assertTrue(self.run_with("verify_transaction", reference="R3").paid)
        self.server.on("GET", status_path("R4"), ok({"status": "021", "message": "not found"}))
        self.assertFalse(self.run_with("verify_transaction", reference="R4").found)


class RemitaWebhookTests(SimpleTestCase):
    def setUp(self):
        self.remita = RemitaConnector()
        self.server = FakeTransport()

    def receive(self, body):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        with use_transport(self.server):
            return self.remita.handle_webhook(raw_body=raw, headers={}, secret=CREDS, environment="test", settings={})

    def test_no_signature_is_documented_so_the_amount_and_identity_come_from_remitas_own_answer(self):
        self.server.on("GET", status_path("R1"), paid("R1", 1500))
        outcome = self.receive([{"rrr": "R1", "amount": 99999999, "payerName": "RANDALL AKANBI", "bank": "058", "debitdate": "2026-09-25 10:30:00", "channel": "POS"}])
        (tx,) = outcome.transactions
        self.assertEqual((tx.external_transaction_id, tx.amount_minor, tx.receiving_account_reference, tx.sender_name), ("R1", 150_000, "R1", "RANDALL AKANBI"))
        self.assertEqual(self.server.called("GET", status_path("R1")), 1)

    def test_a_notification_for_a_reference_remita_does_not_know_counts_for_nothing(self):
        self.server.on("GET", status_path("FORGED"), ok({"status": "021", "message": "not found"}))
        outcome = self.receive([{"rrr": "FORGED", "amount": 500000}])
        self.assertTrue(outcome.ignored and not outcome.transactions)

    def test_a_notification_for_an_unpaid_reference_counts_for_nothing(self):
        self.server.on("GET", status_path("R2"), ok({"RRR": "R2", "status": "021", "amount": 1500}))
        self.assertFalse(self.receive([{"rrr": "R2"}]).transactions)

    def test_a_batch_is_processed_item_by_item(self):
        self.server.on("GET", status_path("R1"), paid("R1", 100)).on("GET", status_path("R2"), paid("R2", 200))
        self.assertEqual([t.amount_minor for t in self.receive([{"rrr": "R1"}, {"rrr": "R2"}, {"nothing": 1}]).transactions], [10_000, 20_000])

    def test_an_unreadable_body_is_refused(self):
        with self.assertRaises(base.ConnectorError):
            self.receive(b"not json")
        with self.assertRaises(base.ConnectorError):
            self.receive([])

    def test_a_remita_outage_while_confirming_is_not_taken_as_paid(self):
        self.server.on("GET", status_path("R1"), base.ProviderUnavailable())
        with self.assertRaises(base.ProviderUnavailable):
            self.receive([{"rrr": "R1"}])
