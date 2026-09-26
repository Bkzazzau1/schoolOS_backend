"""The Monnify connector, against Monnify's own documentation: how it logs in, what it sends to reserve an account, how it reads the
answers, the webhook's signature (production) and the requery that stands in for it (sandbox)."""

import base64
import hashlib
import hmac
import json

from django.test import SimpleTestCase

from ..providers import base
from ..providers.base import CustomerDetails, ProvisionRequest
from ..providers.monnify import SIGNATURE_HEADER, MonnifyConnector, clear_token_cache, to_minor
from ..providers.transport import use_transport
from .fake_transport import FakeTransport, ok

API_KEY, SECRET_KEY, CONTRACT = "MK_TEST_UNIQUEAPIKEY", "UNIQUE-MONNIFY-SECRET-KEY", "7059707855"
CREDS = {"api_key": API_KEY, "secret_key": SECRET_KEY, "contract_code": CONTRACT}
RESERVE = "/api/v2/bank-transfer/reserved-accounts"


def login(token="tok-1", expires=3567):
    return ok({"requestSuccessful": True, "responseMessage": "success", "responseCode": "0", "responseBody": {"accessToken": token, "expiresIn": expires}})


def reserved(number="6000000001", reference="SOS-REF-1", status="ACTIVE"):
    return ok({
        "requestSuccessful": True, "responseMessage": "success", "responseCode": "0",
        "responseBody": {
            "contractCode": CONTRACT, "accountReference": reference, "accountName": "Bello family", "currencyCode": "NGN",
            "customerEmail": "musa@example.com", "customerName": "Musa Bello", "reservationReference": "RSV-77", "status": status,
            "accounts": [{"bankCode": "50515", "bankName": "Moniepoint Microfinance Bank", "accountNumber": number, "accountName": "Bello family"}],
        },
    })


def request(**over):
    fields = dict(
        idempotency_key="k-1", account_reference="SOS-REF-1", family_code="FAM-K7Q2M9XA", family_name="Bello family",
        customer=CustomerDetails(name="Musa Bello", email="musa@example.com", phone="08031111111", bvn="22222222222"), account_name="Bello family",
    )
    fields.update(over)
    return ProvisionRequest(**fields)


def payment(reference="SOS-REF-1", amount="150000.00", status="PAID", product="RESERVED_ACCOUNT", tx="MNFY|20|0001"):
    return {
        "eventType": "SUCCESSFUL_TRANSACTION",
        "eventData": {
            "product": {"reference": reference, "type": product}, "transactionReference": tx, "paymentReference": "PAYREF-1", "paidOn": "2026-09-25 10:30:00.000",
            "paymentDescription": "fees", "amountPaid": amount, "paymentStatus": status, "paymentMethod": "ACCOUNT_TRANSFER", "currency": "NGN",
            "paymentSourceInformation": [{"bankCode": "058", "amountPaid": 150000, "accountName": "RANDALL AKANBI", "sessionId": "SESS-1", "accountNumber": "0123456789"}],
            "destinationAccountInformation": {"bankCode": "50515", "bankName": "Moniepoint", "accountNumber": "6000000001"},
        },
    }


def signed(body: dict, key=SECRET_KEY):
    raw = json.dumps(body).encode()
    return raw, {SIGNATURE_HEADER: hmac.new(key.encode(), raw, hashlib.sha512).hexdigest()}


def query_answer(status="PAID", amount=150000.0, reference="SOS-REF-1", product="RESERVED_ACCOUNT"):
    return ok({"requestSuccessful": True, "responseBody": {
        "transactionReference": "MNFY|20|0001", "paymentStatus": status, "amountPaid": amount, "currency": "NGN", "product": {"type": product, "reference": reference}}})


class MonnifyBase(SimpleTestCase):
    def setUp(self):
        clear_token_cache()
        self.addCleanup(clear_token_cache)
        self.monnify = MonnifyConnector()
        self.server = FakeTransport().on("POST", "/api/v1/auth/login", login())


class MonnifyConnectTests(MonnifyBase):
    def validate(self, credentials=CREDS, environment="test", settings=None):
        with use_transport(self.server):
            return self.monnify.validate_credentials(environment=environment, credentials=credentials, settings=settings or {})

    def test_the_key_pair_is_proved_by_logging_in_with_basic_auth_to_the_right_host(self):
        result = self.validate()
        (call,) = self.server.calls
        self.assertEqual(call.host, "sandbox.monnify.com")
        self.assertEqual(call.headers["Authorization"], "Basic " + base64.b64encode(f"{API_KEY}:{SECRET_KEY}".encode()).decode())
        self.assertEqual(result.settings, {"preferred_bank_code": "50515"})
        self.assertEqual(result.secret, CREDS)
        self.assertEqual(result.merchant.reference, "****7855")  # only the last digits of the contract are ever shown
        self.assertFalse(result.meta["contract_code_verified"])  # Monnify only checks the contract when an account is made

    def test_live_goes_to_the_live_host(self):
        self.validate(environment="live")
        self.assertEqual(self.server.calls[0].host, "api.monnify.com")

    def test_a_key_pair_monnify_refuses_is_bad_credentials(self):
        self.server.on("POST", "/api/v1/auth/login", ok({"requestSuccessful": False, "responseMessage": "Unauthorized"}, 401))
        with self.assertRaises(base.BadCredentials):
            self.validate()

    def test_a_missing_credential_is_refused_before_anything_is_sent(self):
        with self.assertRaises(base.BadCredentials):
            self.validate({"api_key": API_KEY, "secret_key": "", "contract_code": CONTRACT})
        self.assertEqual(self.server.calls, [])

    def test_a_bank_code_must_be_digits(self):
        with self.assertRaises(base.ProviderRejected):
            self.validate(settings={"preferred_bank_code": "wema"})

    def test_a_token_is_reused_until_it_is_about_to_expire(self):
        self.server.on("GET", f"{RESERVE}/SOS-REF-1", reserved())
        with use_transport(self.server):
            for _ in range(3):
                self.monnify.get_collection_account(CREDS, account_ref="SOS-REF-1", environment="test", settings={})
        self.assertEqual(self.server.called("POST", "/api/v1/auth/login"), 1)
        self.assertEqual({c.headers["Authorization"] for c in self.server.to("GET", f"{RESERVE}/SOS-REF-1")}, {"Bearer tok-1"})

    def test_a_token_monnify_no_longer_accepts_is_renewed_once(self):
        self.server.on("POST", "/api/v1/auth/login", [login("old"), login("new")])
        self.server.on("GET", f"{RESERVE}/SOS-REF-1", [ok({"requestSuccessful": False}, 401), reserved()])
        with use_transport(self.server):
            state = self.monnify.get_collection_account(CREDS, account_ref="SOS-REF-1", environment="test", settings={})
        self.assertEqual(state.status, "active")
        self.assertEqual(self.server.called("POST", "/api/v1/auth/login"), 2)

    def test_amounts_are_whole_kobo_or_they_are_refused(self):
        self.assertEqual((to_minor("150000.00"), to_minor(1500.5), to_minor("0.01")), (15_000_000, 150_050, 1))
        for bad in ("12.345", "-1", "abc"):
            with self.assertRaises(base.ProviderRejected):
                to_minor(bad)


class MonnifyProvisionTests(MonnifyBase):
    def setUp(self):
        super().setUp()
        self.server.on("POST", RESERVE, reserved())

    def provision(self, req=None):
        with use_transport(self.server):
            return self.monnify.provision_family_collection_account(CREDS, req or request(), environment="test", settings={"preferred_bank_code": "50515"})

    def test_a_customer_reserved_account_is_asked_for_exactly_as_documented(self):
        account = self.provision()
        (call,) = self.server.to("POST", RESERVE)
        self.assertEqual(call.headers["Authorization"], "Bearer tok-1")
        self.assertEqual(
            call.json,
            {
                "accountReference": "SOS-REF-1", "accountName": "Bello family", "currencyCode": "NGN", "contractCode": CONTRACT,
                "customerEmail": "musa@example.com", "customerName": "Musa Bello", "getAllAvailableBanks": False,
                "preferredBanks": ["50515"], "bvn": "22222222222",
            },
        )
        self.assertEqual(
            (account.account_number, account.provider_account_ref, account.lookup_ref, account.bank_name, account.ready),
            ("6000000001", "SOS-REF-1", "SOS-REF-1", "Moniepoint Microfinance Bank", True),
        )
        self.assertEqual(account.provider_meta["reservation_reference"], "RSV-77")

    def test_an_nin_alone_is_enough(self):
        self.provision(request(customer=CustomerDetails(name="Musa", email="musa@example.com", nin="12345678901")))
        body = self.server.to("POST", RESERVE)[0].json
        self.assertEqual(body["nin"], "12345678901")
        self.assertNotIn("bvn", body)

    def test_without_a_bvn_or_nin_nothing_is_sent_and_the_school_is_told_what_is_missing(self):
        with self.assertRaises(base.ProviderRejected) as caught:
            self.provision(request(customer=CustomerDetails(name="Musa", email="musa@example.com")))
        self.assertEqual(caught.exception.code, "customer_kyc_required")
        self.assertEqual(self.server.calls, [])

    def test_a_payer_without_an_email_is_refused_before_anything_is_sent(self):
        with self.assertRaises(base.ProviderRejected) as caught:
            self.provision(request(customer=CustomerDetails(name="Musa", bvn="22222222222")))
        self.assertEqual(caught.exception.code, "customer_details_missing")
        self.assertEqual(self.server.calls, [])

    def test_after_a_timeout_the_account_that_may_exist_is_read_not_made_again(self):
        self.server.on("POST", RESERVE, base.ProviderUnavailable())
        self.server.on("GET", f"{RESERVE}/SOS-REF-1", reserved(number="6000000042"))
        self.assertEqual(self.provision().account_number, "6000000042")
        self.assertEqual(self.server.called("POST", RESERVE), 1)

    def test_a_reference_monnify_says_is_taken_leads_to_reading_that_account(self):
        self.server.on("POST", RESERVE, ok({"requestSuccessful": False, "responseMessage": "You can not reserve two accounts with the same reference.", "responseCode": "99"}, 400))
        self.server.on("GET", f"{RESERVE}/SOS-REF-1", reserved(number="6000000099"))
        self.assertEqual(self.provision().account_number, "6000000099")

    def test_a_refusal_carries_schoolos_words_not_monnifys(self):
        self.server.on("POST", RESERVE, ok({"requestSuccessful": False, "responseMessage": f"contract {CONTRACT} echo {SECRET_KEY}"}, 400))
        with self.assertRaises(base.ProviderRejected) as caught:
            self.provision()
        for leaked in (CONTRACT, SECRET_KEY):
            self.assertNotIn(leaked, caught.exception.message)

    def test_a_response_without_an_account_number_is_not_treated_as_success(self):
        self.server.on("POST", RESERVE, ok({"requestSuccessful": True, "responseBody": {"accountReference": "SOS-REF-1", "accounts": []}}))
        with self.assertRaises(base.ProviderRejected):
            self.provision()

    def test_an_account_monnify_has_not_activated_is_not_ready(self):
        self.server.on("POST", RESERVE, reserved(status="INACTIVE"))
        self.assertFalse(self.provision().ready)


class MonnifyLifecycleTests(MonnifyBase):
    def run_with(self, name, **kw):
        with use_transport(self.server):
            return getattr(self.monnify, name)(CREDS, environment="test", settings={}, **kw)

    def test_closing_deallocates_the_reserved_account_by_reference(self):
        path = "/api/v1/bank-transfer/reserved-accounts/reference/SOS-REF-1"
        self.server.on("DELETE", path, ok({"requestSuccessful": True, "responseBody": {}}))
        self.run_with("close_collection_account", account_ref="SOS-REF-1")
        self.assertEqual(self.server.called("DELETE", path), 1)

    def test_monnify_documents_no_deactivate_or_reactivate_so_none_is_offered(self):
        for name in ("deactivate_collection_account", "reactivate_collection_account"):
            with self.assertRaises(base.NotSupported):
                self.run_with(name, account_ref="SOS-REF-1")
        c = MonnifyConnector.info.capabilities
        self.assertFalse(c.supports_account_deactivation or c.supports_account_reactivation)
        self.assertTrue(c.supports_account_closure and c.requires_customer_kyc)

    def test_an_account_monnify_no_longer_has_is_reported_closed(self):
        self.server.on("GET", f"{RESERVE}/GONE", ok({"requestSuccessful": False}, 404))
        self.assertEqual(self.run_with("get_collection_account", account_ref="GONE").status, "closed")

    def test_a_payment_is_confirmed_by_asking_monnify(self):
        self.server.on("GET", "/api/v2/merchant/transactions/query", query_answer())
        result = self.run_with("verify_transaction", reference="MNFY|20|0001")
        self.assertEqual((result.found, result.paid, result.amount_minor, result.receiving_reference), (True, True, 15_000_000, "SOS-REF-1"))
        self.assertEqual(self.server.to("GET", "/api/v2/merchant/transactions/query")[0].query, {"transactionReference": "MNFY|20|0001"})
        self.server.on("GET", "/api/v2/merchant/transactions/query", ok({"requestSuccessful": False}, 404))
        self.assertFalse(self.run_with("verify_transaction", reference="NOPE").found)


class MonnifyWebhookTests(MonnifyBase):
    def receive(self, raw, headers, environment="live"):
        with use_transport(self.server):
            return self.monnify.handle_webhook(raw_body=raw, headers=headers, secret=CREDS, environment=environment, settings={})

    def test_a_signed_payment_into_a_family_account_is_normalised_and_identifies_the_account_by_reference(self):
        (tx,) = self.receive(*signed(payment())).transactions
        self.assertEqual(
            (tx.external_transaction_id, tx.amount_minor, tx.receiving_account_reference, tx.sender_name, tx.provider_session_id, tx.transaction_type),
            ("MNFY|20|0001", 15_000_000, "SOS-REF-1", "RANDALL AKANBI", "SESS-1", "ACCOUNT_TRANSFER"),
        )
        self.assertEqual(self.server.calls, [])  # a signed live event needs no further call

    def test_a_forged_signature_is_refused(self):
        raw, headers = signed(payment())
        with self.assertRaises(base.InvalidSignature):
            self.receive(raw, {SIGNATURE_HEADER: "f" * 128})
        with self.assertRaises(base.InvalidSignature):
            self.receive(raw.replace(b"150000.00", b"999999.00"), headers)
        with self.assertRaises(base.InvalidSignature):
            self.receive(*signed(payment(), key="another-merchants-secret"))

    def test_an_unsigned_live_delivery_is_refused_because_live_ones_are_always_signed(self):
        with self.assertRaises(base.InvalidSignature):
            self.receive(json.dumps(payment()).encode(), {}, environment="live")
        self.assertEqual(self.server.calls, [])

    def test_a_sandbox_delivery_is_unsigned_so_nothing_in_it_is_trusted_and_monnify_is_asked(self):
        self.server.on("GET", "/api/v2/merchant/transactions/query", query_answer(amount=1500.0))
        # the body claims a much bigger payment; only Monnify's own answer counts
        (tx,) = self.receive(json.dumps(payment(amount="9999999.00")).encode(), {}, environment="test").transactions
        self.assertEqual(tx.amount_minor, 150_000)
        self.assertEqual(self.server.called("GET", "/api/v2/merchant/transactions/query"), 1)

    def test_a_sandbox_delivery_monnify_has_never_heard_of_is_refused(self):
        self.server.on("GET", "/api/v2/merchant/transactions/query", ok({"requestSuccessful": False}, 404))
        with self.assertRaises(base.InvalidSignature):
            self.receive(json.dumps(payment()).encode(), {}, environment="test")

    def test_a_sandbox_delivery_monnify_says_is_not_paid_counts_for_nothing(self):
        self.server.on("GET", "/api/v2/merchant/transactions/query", query_answer(status="PENDING"))
        outcome = self.receive(json.dumps(payment()).encode(), {}, environment="test")
        self.assertTrue(outcome.ignored and not outcome.transactions)

    def test_something_that_is_not_a_payment_into_a_reserved_account_is_ignored(self):
        self.assertTrue(self.receive(*signed(payment(product="WEB_SDK"))).ignored)
        self.assertTrue(self.receive(*signed({"eventType": "SUCCESSFUL_DISBURSEMENT", "eventData": {}})).ignored)
        self.assertTrue(self.receive(*signed(payment(status="PENDING"))).ignored)

    def test_an_unreadable_body_is_refused_after_the_signature_passes(self):
        raw = b"not json"
        with self.assertRaises(base.ConnectorError):
            self.receive(raw, {SIGNATURE_HEADER: hmac.new(SECRET_KEY.encode(), raw, hashlib.sha512).hexdigest()})
