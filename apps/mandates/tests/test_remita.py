"""Remita Direct Debit, against the shapes Remita's own documentation gives (Direct Debit for Non-Financial Institutions): the requests SchoolOS makes
carry exactly the documented hashes and headers, and its status codes are understood as documented. The real Remita is never called: a scripted
transport answers as the documentation says it does."""

import hashlib
import json
from datetime import date, timedelta

from django.test import override_settings
from django.utils import timezone

from apps.bankconnect.providers.transport import HttpResult, use_transport
from apps.bankconnect.tests.fake_transport import FakeTransport, ok

from .. import connections, execution, jobs, mandate_provider, mandates
from ..constants import DebitOutcome, MandateStatus
from ..models import DirectDebitMandate, MandateTransaction
from ..providers import registry
from ..providers.base import (
    BadCredentials,
    ConnectorError,
    CreateMandateRequest,
    DebitRequest,
    NotSupported,
    PayerDetails,
    ProviderRejected,
    ProviderUnavailable,
)
from ..providers.remita import DEMO_BASE, RemitaMandateConnector
from .base import MandateTestCase, N

BASE = "/remita/exapp/api/v1/send/api/echannelsvc/echannel/mandate"
CREDS = {"merchant_id": "2547916", "service_type_id": "4430731", "api_key": "KEY-REMITA-SECRET", "api_token": "TOKEN-REMITA-SECRET"}
SECRET = {**CREDS, "connection_id": "c-1"}
ARGS = {"environment": "test", "settings": {}}


def sha(*parts) -> str:
    return hashlib.sha512("".join(str(p) for p in parts).encode()).hexdigest()


def jsonp(body: dict) -> HttpResult:
    return HttpResult(200, None, f"jsonp ({json.dumps(body)})")


def not_found() -> HttpResult:
    return ok({"statuscode": "074", "status": "No Available Record", "requestId": "1"})


def scripted() -> FakeTransport:
    """Remita as its documentation describes it, for a school whose credentials are right."""
    server = FakeTransport()
    server.on("POST", BASE + "/status", lambda call: not_found() if call.json["mandateId"] == "000000000000" else ok(
        {"statuscode": "00", "requestId": call.json["requestId"], "mandateId": call.json["mandateId"], "isActive": False, "status": "Successful",
         "startDate": "03/07/2025", "endDate": "03/07/2035", "registrationDate": "03/07/2025"}))
    return server


class RemitaAdapterTests(MandateTestCase):
    def setUp(self):
        super().setUp()
        self.server = scripted()
        context = use_transport(self.server)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.connector = RemitaMandateConnector()

    # -- who it is ------------------------------------------------------------------------------

    def test_remita_is_a_mandate_provider_with_the_capabilities_its_documentation_supports(self):
        self.assertIs(registry.get_connector("remita").__class__, RemitaMandateConnector)
        caps = self.connector.info.capabilities
        self.assertTrue(caps.supports_manual_debit and caps.supports_debit_status and caps.supports_otp_activation and caps.supports_variable_amount_mandate)
        self.assertTrue(caps.requires_account_number_for_debit and caps.supports_webhooks and caps.supports_status_requery)
        self.assertFalse(caps.supports_suspension or caps.supports_reactivation or caps.supports_scheduled_debit or caps.requires_provider_customer)
        self.assertEqual(self.connector.info.webhook.verification, "requery")  # Remita documents no signature on its notifications
        self.assertEqual(self.connector.info.maximum_scope, "calendar_month")
        self.assertEqual([f.name for f in self.connector.info.credential_fields], ["merchant_id", "service_type_id", "api_key", "api_token"])

    def test_there_is_no_invoice_or_rrr_behaviour_at_all(self):
        self.assertFalse(hasattr(self.connector, "provision_family_collection_account"))
        with self.assertRaises(NotSupported):
            self.connector.suspend_mandate(SECRET, mandate_ref="1", **ARGS)
        self.server.on("POST", BASE + "/setup", ok({"statuscode": "040", "mandateId": "1"}))
        self.connector.create_mandate(SECRET, self.create_request(), **ARGS)
        paths = [c.path for c in self.server.calls]
        self.assertFalse(any("paymentinit" in p or "status.reg" in p or "deactivate" in p for p in paths))

    # -- credentials ------------------------------------------------------------------------------

    def test_credentials_are_checked_with_a_status_query_for_a_mandate_that_cannot_exist(self):
        validated = self.connector.validate_credentials(environment="test", credentials=CREDS, settings={})
        self.assertEqual((validated.merchant.display_name, validated.merchant.reference), ("Remita merchant", "****7916"))
        body = self.server.calls[-1].json
        self.assertEqual(body["mandateId"], "000000000000")
        self.assertEqual(body["hash"], sha("000000000000", CREDS["merchant_id"], body["requestId"], CREDS["api_key"]))  # documented: mandateId + merchantId + requestId + apiKey

    def test_a_refused_hash_or_authentication_is_bad_credentials(self):
        for code in ("013", "020"):
            self.server.on("POST", BASE + "/status", ok({"statuscode": code}))
            with self.assertRaises(BadCredentials):
                self.connector.validate_credentials(environment="test", credentials=CREDS, settings={})

    def test_an_answer_that_cannot_be_understood_is_not_a_connection(self):
        self.server.on("POST", BASE + "/status", ok({"statuscode": "999"}))
        with self.assertRaises(ProviderRejected):
            self.connector.validate_credentials(environment="test", credentials=CREDS, settings={})

    def test_a_timeout_is_unavailable_never_a_refusal(self):
        self.server.on("POST", BASE + "/status", ProviderUnavailable())
        with self.assertRaises(ProviderUnavailable):
            self.connector.validate_credentials(environment="test", credentials=CREDS, settings={})

    def test_the_demo_host_is_used_for_test_and_live_is_refused_until_an_operator_sets_it(self):
        self.connector.validate_credentials(environment="test", credentials=CREDS, settings={})
        self.assertTrue(self.server.calls[-1].url.startswith(DEMO_BASE))
        with self.assertRaises(ProviderRejected) as caught:
            self.connector.validate_credentials(environment="live", credentials=CREDS, settings={})
        self.assertEqual(caught.exception.code, "live_not_configured")
        with override_settings(MANDATES_REMITA_LIVE_BASE_URL="https://live.example/remita/exapp/api/v1/send/api"):
            self.connector.validate_credentials(environment="live", credentials=CREDS, settings={})
        self.assertTrue(self.server.calls[-1].url.startswith("https://live.example/"))

    # -- a mandate ------------------------------------------------------------------------------

    def create_request(self, **over):
        values = dict(
            request_ref="1751533484929", payer=PayerDetails("John Doe", "john.doe@mailinator.com", "08082278899"), bank_code="057", account_number="0100034932",
            maximum_amount_minor=500_000 * N, start_date=date(2026, 7, 3), end_date=date(2027, 1, 3), max_debits=6,
        )
        values.update(over)
        return CreateMandateRequest(**values)

    def test_a_mandate_is_set_up_exactly_as_remitas_documentation_says(self):
        self.server.on("POST", BASE + "/setup", lambda call: ok({"statuscode": "040", "requestId": call.json["requestId"], "mandateId": "150798919022", "status": "Initail Request OK"}))
        made = self.connector.create_mandate(SECRET, self.create_request(), **ARGS)
        sent = self.server.calls[-1].json
        self.assertEqual(
            {k: sent[k] for k in ("merchantId", "serviceTypeId", "requestId", "payerName", "payerEmail", "payerPhone", "payerBankCode", "payerAccount", "amount", "startDate", "endDate", "mandateType", "maxNoOfDebits")},
            {"merchantId": "2547916", "serviceTypeId": "4430731", "requestId": "1751533484929", "payerName": "John Doe", "payerEmail": "john.doe@mailinator.com",
             "payerPhone": "08082278899", "payerBankCode": "057", "payerAccount": "0100034932", "amount": "500000", "startDate": "03/07/2026", "endDate": "03/01/2027",
             "mandateType": "DD", "maxNoOfDebits": "6"},
        )
        self.assertEqual(sent["hash"], sha("2547916", "4430731", "1751533484929", "500000", CREDS["api_key"]))  # merchantId + serviceTypeId + requestId + amount + apiKey
        self.assertEqual((made.provider_ref, made.status, made.provider_status_code), ("150798919022", MandateStatus.PENDING_ACTIVATION, "040"))
        self.assertNotIn(CREDS["api_key"], json.dumps(sent))  # the key is only ever in the hash

    def test_amounts_are_plain_naira_strings(self):
        self.server.on("POST", BASE + "/setup", ok({"statuscode": "040", "mandateId": "1"}))
        self.connector.create_mandate(SECRET, self.create_request(maximum_amount_minor=12_550), **ARGS)
        self.assertEqual(self.server.calls[-1].json["amount"], "125.50")

    def test_a_setup_remita_does_not_confirm_is_refused_and_an_unreadable_answer_is_unknown(self):
        self.server.on("POST", BASE + "/setup", ok({"statuscode": "073", "status": "Invalid Mandate Type"}))
        with self.assertRaises(ProviderRejected):
            self.connector.create_mandate(SECRET, self.create_request(), **ARGS)
        self.server.on("POST", BASE + "/setup", HttpResult(200, None, "<html>maintenance</html>"))
        with self.assertRaises(ProviderUnavailable):  # after a request went out: the outcome is not known
            self.connector.create_mandate(SECRET, self.create_request(), **ARGS)

    def test_a_retry_after_a_lost_answer_asks_about_the_mandate_it_already_has(self):
        self.server.on("POST", BASE + "/status", ok({"statuscode": "061", "mandateId": "150798919022", "isActive": False, "status": "Mandate Not Activated"}))
        found = self.connector.create_mandate(SECRET, self.create_request(existing_ref="150798919022"), **ARGS)
        self.assertEqual((found.status, self.server.called("POST", BASE + "/setup")), (MandateStatus.PENDING_ACTIVATION, 0))

    def test_the_status_of_a_mandate_is_asked_with_its_own_request_id_and_understood(self):
        cases = [
            ({"statuscode": "00", "isActive": True, "endDate": "18/01/2035", "status": "Successful"}, MandateStatus.ACTIVE),
            ({"statuscode": "00", "isActive": False, "status": "Successful"}, MandateStatus.PENDING_ACTIVATION),
            ({"statuscode": "061", "status": "Mandate Not Activated"}, MandateStatus.PENDING_ACTIVATION),
            ({"statuscode": "063", "status": "Expired Mandate"}, MandateStatus.EXPIRED),
            ({"statuscode": "066", "status": "Mandate Deactivated"}, MandateStatus.CANCELLED),
            ({"statuscode": "00", "isActive": True, "endDate": "18/01/2001", "status": "Successful"}, MandateStatus.EXPIRED),
        ]
        for answer, expected in cases:
            self.server.on("POST", BASE + "/status", ok({"mandateId": "9", **answer}))
            got = self.connector.get_mandate_status(SECRET, mandate_ref="9", request_ref="1751532065837", **ARGS)
            self.assertEqual(got.status, expected, answer)
        sent = self.server.calls[-1].json
        self.assertEqual(sent["hash"], sha("9", "2547916", "1751532065837", CREDS["api_key"]))

    def test_a_jsonp_wrapped_answer_is_read(self):
        self.server.on("POST", BASE + "/status", jsonp({"statuscode": "00", "mandateId": "9", "isActive": True, "endDate": "18/01/2035", "status": "Successful"}))
        self.assertEqual(self.connector.get_mandate_status(SECRET, mandate_ref="9", request_ref="1", **ARGS).status, MandateStatus.ACTIVE)

    def test_an_unknown_mandate_is_not_found_and_bad_credentials_are_refused(self):
        self.server.on("POST", BASE + "/status", not_found())
        with self.assertRaises(ProviderRejected) as caught:
            self.connector.get_mandate_status(SECRET, mandate_ref="9", request_ref="1", **ARGS)
        self.assertEqual(caught.exception.code, "mandate_not_found")
        self.server.on("POST", BASE + "/status", ok({"statuscode": "013"}))
        with self.assertRaises(BadCredentials):
            self.connector.get_mandate_status(SECRET, mandate_ref="9", request_ref="1", **ARGS)

    # -- activation ------------------------------------------------------------------------------

    def test_otp_activation_carries_the_documented_headers_and_hash(self):
        self.server.on("POST", BASE + "/requestAuthorization", ok({
            "statuscode": "00", "remitaTransRef": "1729968531931", "mandateId": "9", "status": "SUCCESS",
            "authParams": [{"description2": "Please specify the last 4 digits of your bank card", "label1": "One Time Password", "param1": "OTP", "label2": "Card",
                            "description1": "Please enter your Bank OTP", "param2": "CARD"}]}))
        challenge = self.connector.request_activation(SECRET, mandate_ref="9", request_ref="1751532065837", **ARGS)
        call = self.server.calls[-1]
        self.assertEqual(call.json, {"mandateId": "9", "requestId": "1751532065837"})
        headers = call.headers
        self.assertEqual((headers["MERCHANT_ID"], headers["API_KEY"]), ("2547916", CREDS["api_key"]))
        self.assertEqual(headers["API_DETAILS_HASH"], sha(CREDS["api_key"], headers["REQUEST_ID"], CREDS["api_token"]))  # apiKey + requestId + apiToken
        self.assertRegex(headers["REQUEST_TS"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+000000$")
        self.assertEqual(challenge.challenge_ref, "1729968531931")
        self.assertEqual([f["name"] for f in challenge.fields], ["OTP", "CARD"])

    def test_otp_validation_sends_the_answers_the_way_the_documentation_shows_and_then_asks_whether_it_can_be_debited(self):
        self.server.on("POST", BASE + "/validateAuthorization", ok({"statuscode": "00", "mandateId": "9", "status": "Mandate Activated Successfully"}))
        self.server.on("POST", BASE + "/status", ok({"statuscode": "00", "mandateId": "9", "isActive": True, "endDate": "18/01/2035", "status": "Successful"}))
        got = self.connector.confirm_activation(SECRET, mandate_ref="9", challenge_ref="1751532401042", answers={"OTP": "1234", "CARD": "4567"}, request_ref="1", **ARGS)
        self.assertEqual(self.server.calls[-2].json, {"remitaTransRef": "1751532401042", "authParams": [{"param1": "OTP", "value": "1234"}, {"param2": "CARD", "value": "4567"}]})
        self.assertEqual(got.status, MandateStatus.ACTIVE)  # activated AND the status says so

    def test_a_refused_one_time_password_is_a_refusal(self):
        self.server.on("POST", BASE + "/validateAuthorization", ok({"statuscode": "061", "status": "Mandate Not Activated"}))
        with self.assertRaises(ProviderRejected) as caught:
            self.connector.confirm_activation(SECRET, mandate_ref="9", challenge_ref="1", answers={"OTP": "0"}, **ARGS)
        self.assertEqual(caught.exception.code, "activation_refused")

    def test_the_print_form_address_is_the_documented_one_for_the_demo_host(self):
        self.server.on("POST", BASE + "/setup", ok({"statuscode": "040", "mandateId": "150798919022"}))
        made = self.connector.create_mandate(SECRET, self.create_request(), **ARGS)
        url = made.activation["form"]
        self.assertEqual(url, f"https://demo.remita.net/remita/ecomm/mandate/form/2547916/{sha('2547916', CREDS['api_key'], '1751533484929')}/150798919022/1751533484929/rest.reg")

    def test_stopping_a_mandate_is_the_documented_call(self):
        self.server.on("POST", BASE + "/stop", ok({"statuscode": "00", "mandateId": "9", "status": "Successful"}))
        got = self.connector.cancel_mandate(SECRET, mandate_ref="9", request_ref="1751532065837", **ARGS)
        sent = self.server.calls[-1].json
        self.assertEqual((got.status, sent["hash"]), (MandateStatus.CANCELLED, sha("9", "2547916", "1751532065837", CREDS["api_key"])))

    # -- a debit ------------------------------------------------------------------------------

    def debit_request(self, **over):
        values = dict(request_ref="1751533484930", mandate_ref="180799091923", amount_minor=280_000 * N, funding_account="0100034932", funding_bank_code="057")
        values.update(over)
        return DebitRequest(**values)

    def test_a_debit_instruction_is_sent_exactly_as_documented(self):
        self.server.on("POST", BASE + "/payment/send", ok({"statuscode": "069", "RRR": "270007740695", "requestId": "1751533484930", "mandateId": "180799091923", "transactionRef": 7740695, "status": "New Transaction"}))
        got = self.connector.create_debit(SECRET, self.debit_request(), **ARGS)
        sent = self.server.calls[-1].json
        self.assertEqual(
            {k: sent[k] for k in ("merchantId", "serviceTypeId", "requestId", "totalAmount", "mandateId", "fundingAccount", "fundingBankCode")},
            {"merchantId": "2547916", "serviceTypeId": "4430731", "requestId": "1751533484930", "totalAmount": "280000", "mandateId": "180799091923", "fundingAccount": "0100034932", "fundingBankCode": "057"},
        )
        self.assertEqual(sent["hash"], sha("2547916", "4430731", "1751533484930", "280000", CREDS["api_key"]))
        self.assertEqual((got.outcome, got.provider_reference, got.provider_status_code), (DebitOutcome.PENDING, "270007740695", "069"))  # accepted is NOT paid

    def test_only_the_documented_success_code_is_a_successful_debit(self):
        expected = {
            "01": DebitOutcome.SUCCESS, "069": DebitOutcome.PENDING, "070": DebitOutcome.PENDING, "071": DebitOutcome.PENDING, "072": DebitOutcome.PENDING,
            "051": DebitOutcome.FAILED, "107": DebitOutcome.FAILED, "034": DebitOutcome.FAILED, "035": DebitOutcome.FAILED, "061": DebitOutcome.FAILED,
            "062": DebitOutcome.FAILED, "063": DebitOutcome.FAILED, "066": DebitOutcome.FAILED, "067": DebitOutcome.FAILED, "073": DebitOutcome.FAILED,
            "074": DebitOutcome.NOT_FOUND, "00": DebitOutcome.UNKNOWN, "075": DebitOutcome.UNKNOWN, "999": DebitOutcome.UNKNOWN,
        }
        for code, outcome in expected.items():
            self.server.on("POST", BASE + "/payment/status", ok({"statuscode": code, "status": "x", "amount": "3000", "RRR": "270007740695"}))
            got = self.connector.get_debit_status(SECRET, mandate_ref="9", request_ref="73298", **ARGS)
            self.assertEqual(got.outcome, outcome, code)
        self.assertEqual([c for c, o in expected.items() if o == DebitOutcome.SUCCESS], ["01"])

    def test_failure_codes_are_words_a_person_can_act_on(self):
        for code, word in (("051", "insufficient_funds"), ("035", "payment_limit_exceeded"), ("061", "mandate_not_activated"), ("063", "mandate_expired")):
            self.server.on("POST", BASE + "/payment/status", ok({"statuscode": code, "status": "x"}))
            self.assertEqual(self.connector.get_debit_status(SECRET, mandate_ref="9", request_ref="1", **ARGS).failure_code, word)

    def test_a_debit_status_is_asked_by_the_debits_own_request_id_with_the_documented_hash(self):
        self.server.on("POST", BASE + "/payment/status", ok({"statuscode": "070", "status": "Awaiting Debit"}))
        self.connector.get_debit_status(SECRET, mandate_ref="180799091923", request_ref="73298", **ARGS)
        sent = self.server.calls[-1].json
        self.assertEqual((sent["requestId"], sent["mandateId"], sent["hash"]), ("73298", "180799091923", sha("180799091923", "2547916", "73298", CREDS["api_key"])))

    def test_a_debit_that_times_out_or_cannot_be_read_is_unknown_not_failed(self):
        self.server.on("POST", BASE + "/payment/send", ProviderUnavailable())
        with self.assertRaises(ProviderUnavailable):
            self.connector.create_debit(SECRET, self.debit_request(), **ARGS)
        self.server.on("POST", BASE + "/payment/send", HttpResult(200, None, "garbled"))
        with self.assertRaises(ProviderUnavailable):
            self.connector.create_debit(SECRET, self.debit_request(), **ARGS)
        self.server.on("POST", BASE + "/payment/send", HttpResult(503, None, ""))
        with self.assertRaises(ProviderUnavailable):
            self.connector.create_debit(SECRET, self.debit_request(), **ARGS)

    def test_remitas_authentication_errors_on_a_debit_are_bad_credentials(self):
        self.server.on("POST", BASE + "/payment/send", ok({"statuscode": "020"}))
        with self.assertRaises(BadCredentials):
            self.connector.create_debit(SECRET, self.debit_request(), **ARGS)

    # -- notifications ------------------------------------------------------------------------------

    def test_a_notification_says_only_what_to_ask_about_and_carries_no_signature(self):
        body = json.dumps({"notificationType": "DEBIT", "lineItems": [{"mandateId": "180799091923", "debitDate": "03/05/2017", "requestId": "73298", "amount": "10000"}]}).encode()
        out = self.connector.handle_webhook(raw_body=body, headers={}, secret=SECRET, environment="test", settings={})
        self.assertEqual([(e.kind, e.mandate_ref, e.request_ref) for e in out.events], [("debit", "180799091923", "73298")])
        body = json.dumps({"notificationType": "ACTIVATION", "lineItems": [{"mandateId": "9", "requestId": "1", "amount": "10000"}]}).encode()
        self.assertEqual([e.kind for e in self.connector.handle_webhook(raw_body=body, headers={}, secret=SECRET, environment="test", settings={}).events], ["mandate"])

    def test_other_or_unreadable_notifications_are_ignored_or_refused(self):
        self.assertTrue(self.connector.handle_webhook(raw_body=b'{"notificationType": "OTHER"}', headers={}, secret=SECRET, environment="test", settings={}).ignored)
        self.assertTrue(self.connector.handle_webhook(raw_body=b'{"notificationType": "DEBIT", "lineItems": []}', headers={}, secret=SECRET, environment="test", settings={}).ignored)
        with self.assertRaises(ConnectorError):
            self.connector.handle_webhook(raw_body=b"not json", headers={}, secret=SECRET, environment="test", settings={})


class RemitaEndToEndTests(MandateTestCase):
    """The whole path through SchoolOS with Remita's documented server scripted: connect, a payer's mandate, activation, an approved debit, settlement."""

    def setUp(self):
        super().setUp()
        self.server = scripted()
        self.server.on("POST", BASE + "/setup", lambda call: ok({"statuscode": "040", "requestId": call.json["requestId"], "mandateId": "M-" + call.json["requestId"][-6:], "status": "Initail Request OK"}))
        context = use_transport(self.server)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.remita = connections.connect(self.owner, provider="remita", environment="test", label="Remita fees", credentials=dict(CREDS))
        self.family = self.make_family("Bello", owes=280_000)

    def make_active(self, family=None, account="0100034932"):
        family = family or self.family
        mandate = self.start_mandate(family, connection=self.remita, account=account, bank="057", max_debits=6)
        self.server.on("POST", BASE + "/status", ok({"statuscode": "00", "mandateId": mandate.provider_mandate_reference, "isActive": True, "endDate": "18/01/2035", "status": "Successful"}))
        return mandate_provider.refresh_mandate(mandate.id)

    def test_a_remita_mandate_is_made_and_becomes_debit_ready_only_when_remita_says_it_is_active(self):
        mandate = self.start_mandate(self.family, connection=self.remita, account="0100034932", bank="057")
        self.assertEqual((mandate.status, mandate.provider, mandate.bank_name), (MandateStatus.PENDING_ACTIVATION, "remita", "Zenith Bank"))
        self.assertFalse(mandates.is_debit_ready(mandate)[0])
        self.server.on("POST", BASE + "/status", ok({"statuscode": "00", "mandateId": mandate.provider_mandate_reference, "isActive": True, "endDate": "18/01/2035", "status": "Successful"}))
        active = mandate_provider.refresh_mandate(mandate.id)
        self.assertEqual((active.status, active.provider_status_code, mandates.is_debit_ready(active)[0]), (MandateStatus.ACTIVE, "00", True))
        self.assertIsNotNone(active.debit_ready_at)

    def test_remita_keeps_the_sealed_account_for_debits_and_removes_it_when_the_mandate_ends(self):
        active = self.make_active()
        self.assertTrue(DirectDebitMandate.objects.get(pk=active.pk).sealed_account_details)
        self.server.on("POST", BASE + "/stop", ok({"statuscode": "00", "mandateId": active.provider_mandate_reference, "status": "Successful"}))
        mandate_provider.cancel(self.manager, active.id)
        self.assertEqual(bytes(DirectDebitMandate.objects.get(pk=active.pk).sealed_account_details), b"")

    def test_an_approved_debit_is_sent_with_the_payers_funding_account_and_is_settled_only_when_remita_says_paid(self):
        active = self.make_active()
        self.server.on("POST", BASE + "/payment/send", lambda call: ok({"statuscode": "069", "RRR": "270007740695", "requestId": call.json["requestId"], "mandateId": call.json["mandateId"], "transactionRef": 7740695, "status": "New Transaction"}))
        batch = self.approved()
        execution.start(self.maker, batch.id)
        jobs.drain()
        sent = self.server.to("POST", BASE + "/payment/send")[0].json
        self.assertEqual((sent["fundingAccount"], sent["fundingBankCode"], sent["totalAmount"], sent["mandateId"]), ("0100034932", "057", "280000", active.provider_mandate_reference))
        self.assertEqual((self.owed(self.family), MandateTransaction.objects.get().status), (280_000 * N, DebitOutcome.PENDING))  # accepted is not paid
        self.server.on("POST", BASE + "/payment/status", ok({"statuscode": "01", "status": "Approved", "amount": "280000", "RRR": "270007740695", "requestId": sent["requestId"], "mandateId": active.provider_mandate_reference}))
        jobs.drain(now_fn=lambda: timezone.now() + timedelta(hours=1))
        self.assertEqual(self.owed(self.family), 0)
        self.assertEqual(MandateTransaction.objects.get().provider_transaction_reference, "270007740695")
        self.assertLedgerHolds(self.family)

    def test_insufficient_funds_fails_the_debit_and_changes_nothing_in_the_ledger(self):
        self.make_active()
        self.server.on("POST", BASE + "/payment/send", ok({"statuscode": "051", "status": "No sufficient funds", "requestId": "1", "mandateId": "x"}))
        batch = self.run_batch(self.approved())
        item = self.item(batch, self.family)
        self.assertEqual((item.error_code, self.owed(self.family)), ("insufficient_funds", 280_000 * N))

    def test_a_remita_timeout_is_asked_about_and_never_sent_again(self):
        self.make_active()
        self.server.on("POST", BASE + "/payment/send", ProviderUnavailable())
        self.server.on("POST", BASE + "/payment/status", [ok({"statuscode": "074", "status": "No Available Record"}), ok({"statuscode": "01", "status": "Approved", "amount": "280000", "RRR": "R1"})])
        batch = self.approved()
        execution.start(self.maker, batch.id)
        jobs.run_next()  # unknown
        self.assertEqual(self.server.called("POST", BASE + "/payment/send"), 1)
        self.server.on("POST", BASE + "/payment/send", ok({"statuscode": "069", "RRR": "R1", "status": "New Transaction", "requestId": self.item(batch, self.family).request_ref}))
        jobs.drain(now_fn=lambda: timezone.now() + timedelta(hours=1))  # asked: 074 (no record), so sent again with the same reference; then asked: 01
        sends = self.server.to("POST", BASE + "/payment/send")
        self.assertEqual(len({c.json["requestId"] for c in sends}), 1)  # always the same reference
        self.assertEqual(self.owed(self.family), 0)

    def test_credentials_never_appear_in_anything_stored_or_shown(self):
        from ..models import MandateAuditEvent
        from ..serializers import connection as serialize_connection

        self.make_active()
        blob = json.dumps(serialize_connection(self.remita)) + json.dumps([e.detail for e in MandateAuditEvent.objects.all()])
        for secret in (CREDS["api_key"], CREDS["api_token"], "0100034932"):
            self.assertNotIn(secret, blob)
