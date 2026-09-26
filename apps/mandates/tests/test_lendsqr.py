"""Lendsqr Direct Debit, against the shapes Lendsqr's published documentation gives (the Developer APIs pages and the Adjutor API reference): a mandate
is made for a Lendsqr customer, the payer activates it with a transfer to a NIBSS account, and it is only debit-ready after the bank has finished
setting it up. Lendsqr documents no way for SchoolOS to send a debit, so none is guessed. The real Lendsqr is never called."""

import json
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from django.test import override_settings

from apps.bankconnect.providers.transport import HttpResult, use_transport
from apps.bankconnect.tests.fake_transport import FakeTransport, ok

from .. import connections, mandate_provider, mandates
from ..constants import MandateStatus
from ..models import MandateDebitInstruction
from ..providers import registry
from ..providers.base import (
    BadCredentials,
    CreateMandateRequest,
    DebitRequest,
    PayerDetails,
    PendingDocumentation,
    ProviderRejected,
    ProviderUnavailable,
)
from ..providers.lendsqr import LendsqrMandateConnector
from .base import N, MandateTestCase

API = "/v2"
BANKS = f"{API}/direct-debit/banks"
CUSTOMER = "1000"
LIST = f"{API}/customers/{CUSTOMER}/direct-debit-mandates"
CREATE = f"{API}/customers/direct-debit-mandates"
SECRET = {"api_key": "LENDSQR-API-KEY-SECRET", "connection_id": "c-1"}
ARGS = {"environment": "test", "settings": {}}
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)

BANK_ROWS = {"status": "success", "message": "success", "data": {"data": [
    {"id": 1, "name": "Access Bank", "bank_code": "044", "institution_code": "000014", "url": "x", "activation_amount": "50.00",
     "meta": json.dumps({"mandate-activation-amount": 50, "mandate-activation-bank": "Paystack-Titan", "mandate-activation-account-number": "9880218357"})},
    {"id": 10, "name": "Fidelity Bank", "bank_code": "070", "activation_amount": "100.00",
     "meta": json.dumps({"mandate-activation-amount": 100, "mandate-activation-bank": "Fidelity Bank Plc", "mandate-activation-account-number": "9020025928"})},
]}}


def mandate_json(**over) -> dict:
    row = {
        "id": 16391, "org_id": 300000, "user_id": 1000, "amount": 500000, "mandate_type": "NIBSS-EASYPAY", "activation_type": "emandate", "is_active": 0,
        "status": "pending_mandate_activation", "reference": "fed646d9-4b5f-4fee-b957-03ebb03935f4", "start_date": "2026-09-26T00:00:00.000Z",
        "end_date": "2028-09-26T00:00:00.000Z", "registration_date": "2026-09-26T10:00:00.000Z", "activation_date": None, "payer_bank_code": "044",
        "payer_account": "0123456789", "mandate_id": "RC00001/1007/000404907", "provider_status": None, "last_activity_date": None, "created_on": "2026-09-26T10:00:00.000Z",
    }
    row.update(over)
    return row


def scripted() -> FakeTransport:
    return FakeTransport().on("GET", BANKS, ok(BANK_ROWS)).on("GET", LIST, ok({"status": "success", "data": {"mandates": []}}))


class LendsqrAdapterTests(MandateTestCase):
    def setUp(self):
        super().setUp()
        self.server = scripted()
        context = use_transport(self.server)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.connector = LendsqrMandateConnector()

    def create_request(self, **over):
        values = dict(
            request_ref="1", payer=PayerDetails("Bello Parent"), bank_code="044", account_number="0123456789", maximum_amount_minor=500_000 * N,
            start_date=date(2026, 9, 26), end_date=date(2027, 9, 26), provider_customer_ref=CUSTOMER,
        )
        values.update(over)
        return CreateMandateRequest(**values)

    # -- who it is ------------------------------------------------------------------------------

    def test_lendsqr_is_a_mandate_provider_that_can_make_and_watch_a_mandate_but_not_debit(self):
        self.assertIs(registry.get_connector("lendsqr").__class__, LendsqrMandateConnector)
        caps = self.connector.info.capabilities
        self.assertTrue(caps.supports_transfer_activation and caps.supports_suspension and caps.supports_reactivation and caps.supports_remote_cancellation)
        self.assertTrue(caps.requires_provider_customer and caps.supports_status_requery)
        self.assertFalse(caps.supports_manual_debit or caps.supports_debit_status or caps.supports_webhooks or caps.supports_otp_activation)
        self.assertFalse(caps.requires_account_number_for_debit)

    def test_a_debit_is_refused_as_pending_documentation_and_nothing_is_called(self):
        with self.assertRaises(PendingDocumentation) as caught:
            self.connector.create_debit(SECRET, DebitRequest(request_ref="1", mandate_ref="9", amount_minor=100), **ARGS)
        self.assertEqual(caught.exception.code, "pending_documentation")
        with self.assertRaises(PendingDocumentation):
            self.connector.get_debit_status(SECRET, mandate_ref="9", request_ref="1", **ARGS)
        self.assertEqual(self.server.calls, [])
        with self.assertRaises(Exception):
            self.connector.handle_webhook(raw_body=b"{}", headers={}, secret=SECRET, environment="test", settings={})  # Lendsqr documents no callback events

    # -- credentials and live ---------------------------------------------------------------------

    def test_the_key_is_checked_with_the_documented_banks_call_as_a_bearer_token(self):
        validated = self.connector.validate_credentials(environment="test", credentials={"api_key": SECRET["api_key"]}, settings={})
        call = self.server.calls[-1]
        self.assertEqual((call.method, call.path, call.headers["Authorization"]), ("GET", BANKS, "Bearer LENDSQR-API-KEY-SECRET"))
        self.assertTrue(call.url.startswith("https://adjutor.lendsqr.com/v2/"))
        self.assertRegex(validated.merchant.reference, r"^app-[0-9a-f]{4}$")
        self.assertNotIn(SECRET["api_key"], validated.merchant.reference)

    def test_a_refused_key_is_bad_credentials_and_a_server_error_is_unavailable(self):
        self.server.on("GET", BANKS, HttpResult(401, {"status": "error", "message": "We couldn't verify your access"}))
        with self.assertRaises(BadCredentials):
            self.connector.validate_credentials(environment="test", credentials={"api_key": "x"}, settings={})
        self.server.on("GET", BANKS, HttpResult(503, None, ""))
        with self.assertRaises(ProviderUnavailable):
            self.connector.validate_credentials(environment="test", credentials={"api_key": "x"}, settings={})

    def test_live_is_refused_until_an_operator_has_switched_it_on_and_test_mode_is_always_available(self):
        self.connector.validate_credentials(environment="test", credentials={"api_key": "x"}, settings={})
        with self.assertRaises(ProviderRejected) as caught:
            self.connector.validate_credentials(environment="live", credentials={"api_key": "x"}, settings={})
        self.assertEqual(caught.exception.code, "live_not_enabled")
        self.assertIn("licensed", caught.exception.message)  # SchoolOS does not claim a school is eligible
        with override_settings(MANDATES_LENDSQR_LIVE_ENABLED=True):
            self.connector.validate_credentials(environment="live", credentials={"api_key": "x"}, settings={})

    def test_every_operation_is_refused_live_while_live_is_off(self):
        with self.assertRaises(ProviderRejected):
            self.connector.get_mandate_status(SECRET, mandate_ref="1", environment="live", settings={}, provider_customer_ref=CUSTOMER)

    def test_the_banks_come_with_their_activation_amount_and_the_nibss_account(self):
        banks = {b.code: b for b in self.connector.list_supported_banks(SECRET, **ARGS)}
        self.assertEqual((banks["044"].name, banks["044"].activation_amount_minor), ("Access Bank", 5000))
        self.assertEqual((banks["044"].meta["activationBank"], banks["044"].meta["activationAccount"]), ("Paystack-Titan", "9880218357"))
        self.assertEqual(banks["070"].activation_amount_minor, 10_000)  # some banks need N100

    # -- a mandate ------------------------------------------------------------------------------

    def test_a_mandate_is_made_for_a_lendsqr_customer_exactly_as_documented_and_the_code_is_kept(self):
        self.server.on("POST", CREATE, ok({"status": "success", "message": "Mandate created successfully", "data": mandate_json()}))
        made = self.connector.create_mandate(SECRET, self.create_request(), **ARGS)
        sent = self.server.to("POST", CREATE)[0].json
        self.assertEqual(sent, {"account_number": "0123456789", "bank_code": "044", "amount": 500000.0, "start_date": "2026-09-26", "user_id": "1000"})
        self.assertEqual((made.provider_ref, made.mandate_code, made.status), ("16391", "RC00001/1007/000404907", MandateStatus.PENDING_ACTIVATION))
        self.assertFalse(made.is_active)

    def test_the_activation_instructions_come_from_lendsqr_not_from_schoolos_code(self):
        self.server.on("POST", CREATE, ok({"status": "success", "data": mandate_json()}))
        made = self.connector.create_mandate(SECRET, self.create_request(), **ARGS)
        self.assertEqual(made.activation["method"], "transfer")
        self.assertEqual((made.activation["toBank"], made.activation["toAccount"], made.activation["amountMinor"]), ("Paystack-Titan", "9880218357", 5000))
        self.assertEqual(made.activation["windowHours"], 168)

    def test_the_activation_deadline_uses_the_configured_window_and_never_a_number_in_business_code(self):
        self.server.on("POST", CREATE, ok({"status": "success", "data": mandate_json()}))
        made = self.connector.create_mandate(SECRET, self.create_request(), **ARGS)
        self.assertEqual(made.activation_deadline, datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc))  # registered + 168 hours
        with override_settings(MANDATES_LENDSQR_ACTIVATION_WINDOW_HOURS=336):
            again = self.connector.create_mandate(SECRET, self.create_request(), **ARGS)
        self.assertEqual(again.activation_deadline, datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc))

    def test_a_mandate_needs_the_payers_lendsqr_customer_id(self):
        with self.assertRaises(ProviderRejected) as caught:
            self.connector.create_mandate(SECRET, self.create_request(provider_customer_ref=""), **ARGS)
        self.assertEqual(caught.exception.code, "provider_customer_required")
        self.assertEqual(self.server.to("POST", CREATE), [])

    def test_a_mandate_an_earlier_attempt_already_made_is_adopted_not_duplicated(self):
        self.server.on("GET", LIST, ok({"status": "success", "data": {"mandates": [mandate_json()]}}))
        made = self.connector.create_mandate(SECRET, self.create_request(), **ARGS)
        self.assertEqual((made.provider_ref, self.server.called("POST", CREATE)), ("16391", 0))
        cancelled = mandate_json(status="cancelled")
        self.server.on("GET", LIST, ok({"status": "success", "data": {"mandates": [cancelled]}}))
        self.server.on("POST", CREATE, ok({"status": "success", "data": mandate_json(id=99)}))
        self.assertEqual(self.connector.create_mandate(SECRET, self.create_request(), **ARGS).provider_ref, "99")  # a cancelled one does not block a new one

    # -- status ------------------------------------------------------------------------------

    def status_of(self, now=NOW, **fields):
        self.server.on("GET", LIST, ok({"status": "success", "data": {"mandates": [mandate_json(**fields)]}}))
        with mock.patch("apps.mandates.providers.lendsqr.datetime") as clock:
            clock.now.return_value = now
            clock.fromisoformat = datetime.fromisoformat
            return self.connector.get_mandate_status(SECRET, mandate_ref="16391", provider_customer_ref=CUSTOMER, **ARGS)

    def test_pending_mandate_activation_means_the_payer_has_not_activated_it(self):
        got = self.status_of(status="pending_mandate_activation")
        self.assertEqual((got.status, got.activation_deadline is not None), (MandateStatus.PENDING_ACTIVATION, True))

    def test_pending_means_the_transfer_arrived_and_lendsqr_is_confirming_it(self):
        self.assertEqual(self.status_of(status="pending").status, MandateStatus.ACTIVATING)

    def test_activated_is_not_debit_ready_until_the_banks_setup_time_has_passed(self):
        soon = self.status_of(status="active", is_active=1, activation_date="2026-09-26T11:30:00.000Z")  # 30 minutes ago
        self.assertEqual((soon.status, soon.debit_ready_at), (MandateStatus.PENDING_PROVIDER_SETUP, datetime(2026, 9, 26, 13, 30, tzinfo=timezone.utc)))
        later = self.status_of(status="successful", is_active=1, activation_date="2026-09-26T09:00:00.000Z")  # three hours ago
        self.assertEqual(later.status, MandateStatus.ACTIVE)
        self.assertTrue(later.is_active)

    def test_the_setup_time_is_lendsqrs_rule_kept_in_configuration(self):
        with override_settings(MANDATES_LENDSQR_DEBIT_SETUP_MINUTES=10):
            self.assertEqual(self.status_of(status="active", is_active=1, activation_date="2026-09-26T11:30:00.000Z").status, MandateStatus.ACTIVE)
        self.assertEqual(self.status_of(status="active", is_active=1, activation_date="2026-09-26T11:30:00.000Z").status, MandateStatus.PENDING_PROVIDER_SETUP)

    def test_an_active_mandate_with_no_activation_date_is_still_setting_up_not_ready(self):
        self.assertEqual(self.status_of(status="active", is_active=1, activation_date=None, last_activity_date=None).status, MandateStatus.PENDING_PROVIDER_SETUP)

    def test_a_deactivated_cancelled_or_ended_mandate_is_understood(self):
        self.assertEqual(self.status_of(status="successful", is_active=0, activation_date="2026-09-20T09:00:00.000Z").status, MandateStatus.SUSPENDED)
        self.assertEqual(self.status_of(status="cancelled").status, MandateStatus.CANCELLED)
        self.assertEqual(self.status_of(status="active", is_active=1, activation_date="2026-01-01T09:00:00.000Z", end_date="2026-09-01T00:00:00.000Z").status, MandateStatus.EXPIRED)
        self.assertEqual(self.status_of(status="failed").status, MandateStatus.FAILED)

    def test_a_status_it_does_not_understand_is_refused_not_guessed(self):
        with self.assertRaises(ProviderRejected) as caught:
            self.status_of(status="quantum")
        self.assertEqual(caught.exception.code, "provider_unreadable")

    def test_a_mandate_lendsqr_has_no_record_of_is_not_found(self):
        self.server.on("GET", LIST, ok({"status": "success", "data": {"mandates": []}}))
        with self.assertRaises(ProviderRejected) as caught:
            self.connector.get_mandate_status(SECRET, mandate_ref="16391", provider_customer_ref=CUSTOMER, **ARGS)
        self.assertEqual(caught.exception.code, "mandate_not_found")

    def test_cancel_deactivate_and_activate_are_the_documented_calls(self):
        for action, method in (("cancel", "cancel_mandate"), ("deactivate", "suspend_mandate"), ("activate", "reactivate_mandate")):
            self.server.on("PATCH", f"{API}/customers/direct-debit-mandates/16391/{action}", ok({"status": "success", "data": {"mandate": mandate_json(
                status="cancelled" if action == "cancel" else "successful", is_active=0 if action != "activate" else 1, activation_date="2026-09-01T00:00:00.000Z")}}))
            got = getattr(self.connector, method)(SECRET, mandate_ref="16391", **ARGS)
            self.assertEqual(self.server.calls[-1].path, f"{API}/customers/direct-debit-mandates/16391/{action}")
            self.assertEqual(got.status, {"cancel": MandateStatus.CANCELLED, "deactivate": MandateStatus.SUSPENDED, "activate": MandateStatus.ACTIVE}[action])


class LendsqrThroughSchoolOSTests(MandateTestCase):
    def setUp(self):
        super().setUp()
        self.server = scripted()
        self.server.on("POST", CREATE, ok({"status": "success", "data": mandate_json()}))
        context = use_transport(self.server)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.lendsqr = connections.connect(self.owner, provider="lendsqr", environment="test", label="Lendsqr", credentials={"api_key": SECRET["api_key"]})
        self.family = self.make_family("Bello", owes=280_000)

    def test_a_lendsqr_mandate_needs_the_customer_id_records_it_and_reuses_it_for_the_payers_next_one(self):
        with self.assertRaises(Exception) as caught:
            self.start_mandate(self.family, connection=self.lendsqr, bank="044")
        self.assertEqual(caught.exception.code, "provider_customer_required")
        mandate = self.start_mandate(self.family, connection=self.lendsqr, bank="044", provider_customer_ref=CUSTOMER)
        self.assertEqual((mandate.provider, mandate.mandate_code, mandate.provider_customer_ref, mandate.bank_name), ("lendsqr", "RC00001/1007/000404907", CUSTOMER, "Access Bank"))
        self.assertEqual(mandate.activation_details["toAccount"], "9880218357")
        self.assertIsNotNone(mandate.activation_deadline)
        self.server.on("PATCH", f"{API}/customers/direct-debit-mandates/16391/cancel", ok({"status": "success", "data": {"mandate": mandate_json(status="cancelled")}}))
        self.assertEqual(mandate_provider.cancel(self.manager, mandate.id).status, MandateStatus.CANCELLED)

    def test_lendsqr_needs_no_account_number_after_the_mandate_is_made(self):
        mandate = self.start_mandate(self.family, connection=self.lendsqr, bank="044", provider_customer_ref=CUSTOMER)
        self.server.on("GET", LIST, ok({"status": "success", "data": {"mandates": [mandate_json(status="pending")]}}))
        activating = mandate_provider.refresh_mandate(mandate.id)
        self.assertEqual(activating.status, MandateStatus.ACTIVATING)
        self.assertIsNotNone(activating.consent_at)  # the provider's own activation is the payer's authorisation
        self.assertEqual(bytes(type(activating).objects.get(pk=activating.pk).sealed_account_details), b"")  # not needed again, so removed

    def test_a_mandate_the_bank_is_still_setting_up_is_not_debit_ready_and_a_lendsqr_mandate_cannot_be_offered_a_debit(self):
        mandate = self.start_mandate(self.family, connection=self.lendsqr, bank="044", provider_customer_ref=CUSTOMER)
        recent = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        self.server.on("GET", LIST, ok({"status": "success", "data": {"mandates": [mandate_json(status="active", is_active=1, activation_date=recent)]}}))
        setting_up = mandate_provider.refresh_mandate(mandate.id)
        self.assertEqual(setting_up.status, MandateStatus.PENDING_PROVIDER_SETUP)
        self.assertFalse(mandates.is_debit_ready(setting_up)[0])
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        self.server.on("GET", LIST, ok({"status": "success", "data": {"mandates": [mandate_json(status="active", is_active=1, activation_date=old)]}}))
        ready = mandate_provider.refresh_mandate(mandate.id)
        self.assertEqual(ready.status, MandateStatus.ACTIVE)
        ok_, why = mandates.is_debit_ready(ready)
        self.assertEqual(ok_, False)  # debit-ready at the bank, but SchoolOS cannot send a debit through Lendsqr
        self.assertIn("cannot send a debit", why)
        batch = self.new_batch()
        item = self.item(batch, self.family)
        self.assertEqual((item.eligibility_status, item.selected), ("provider_cannot_debit", False))

    def test_a_debit_claimed_possible_for_lendsqr_still_fails_safely_and_calls_nothing(self):
        from dataclasses import replace

        mandate = self.start_mandate(self.family, connection=self.lendsqr, bank="044", provider_customer_ref=CUSTOMER)
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        self.server.on("GET", LIST, ok({"status": "success", "data": {"mandates": [mandate_json(status="active", is_active=1, activation_date=old)]}}))
        mandate_provider.refresh_mandate(mandate.id)
        connector = LendsqrMandateConnector()
        pretend = replace(connector.info.capabilities, supports_manual_debit=True)
        with mock.patch.object(type(connector), "info", replace(connector.info, capabilities=pretend)):
            batch = self.approved()
            from .. import execution, jobs

            execution.start(self.maker, batch.id)
            jobs.drain()
        item = MandateDebitInstruction.objects.get(batch=batch, family=self.family)
        self.assertEqual((item.status, item.error_code), ("failed", "pending_documentation"))
        self.assertEqual(self.owed(self.family), 280_000 * N)
        self.assertLessEqual({c.path for c in self.server.calls}, {BANKS, LIST, CREATE})  # nothing that could be a debit was ever called

    def test_both_providers_can_be_connected_together_with_no_active_one(self):
        remita_server_needed = FakeTransport()
        remita_server_needed.on("POST", "/remita/exapp/api/v1/send/api/echannelsvc/echannel/mandate/status", ok({"statuscode": "074"}))
        self.server.routes.update(remita_server_needed.routes)
        remita = connections.connect(self.owner, provider="remita", environment="test", credentials={"merchant_id": "1", "service_type_id": "2", "api_key": "k", "api_token": "t"})
        rows = [c for c in connections.list_connections(self.owner)]
        self.assertEqual({c.provider for c in rows}, {"sandbox", "lendsqr", "remita"})
        self.assertTrue(all(c.status == "connected" for c in rows))
        self.assertFalse(any(hasattr(c, "is_active_provider") for c in rows))
        self.assertEqual(remita.provider, "remita")
