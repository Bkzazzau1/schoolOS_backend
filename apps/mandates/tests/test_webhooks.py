"""A provider's callback about a mandate or a debit: a public route that has to defend itself. Remita documents no signature on its notifications, so a
body is never believed: it only names what to ask Remita about, and Remita's own answer decides. Nothing here can put money in a ledger by itself."""

import json

from apps.bankconnect.models import BankTransaction
from apps.bankconnect.providers.transport import HttpResult, use_transport
from apps.bankconnect.tests.fake_transport import FakeTransport, ok

from .. import connections, execution, jobs, mandate_provider
from ..constants import DebitOutcome, MandateStatus
from ..models import MandateProviderEvent, MandateTransaction
from .base import N, MandateTestCase
from .test_remita import BASE, CREDS, scripted


class CallbackTests(MandateTestCase):
    def setUp(self):
        super().setUp()
        self.server = scripted()
        self.server.on("POST", BASE + "/setup", lambda call: ok({"statuscode": "040", "mandateId": "M-" + call.json["requestId"][-6:], "requestId": call.json["requestId"]}))
        context = use_transport(self.server)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.remita = connections.connect(self.owner, provider="remita", environment="test", credentials=dict(CREDS))
        self.path = "/api/v1/" + connections.webhook_setup(self.owner, self.remita.id)["path"]
        self.family = self.make_family("Bello", owes=280_000)
        self.mandate = self.start_mandate(self.family, connection=self.remita, account="0100034932", bank="057", max_debits=6)
        self.client.force_authenticate(None)  # a provider has no sign-in

    def post(self, body, path=None):
        raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
        return self.client.post(path or self.path, data=raw, content_type="application/json")

    def says_active(self):
        self.server.on("POST", BASE + "/status", ok({"statuscode": "00", "mandateId": self.mandate.provider_mandate_reference, "isActive": True, "endDate": "18/01/2035", "status": "Successful"}))

    def activation(self, ref=None):
        return {"notificationType": "ACTIVATION", "lineItems": [{"mandateId": ref or self.mandate.provider_mandate_reference, "activationDate": "19/01/2026", "requestId": "1", "amount": "10000"}]}

    def test_an_activation_notification_makes_schoolos_ask_remita_and_remitas_answer_moves_the_mandate(self):
        self.says_active()
        response = self.post(self.activation())
        self.assertEqual((response.status_code, response.content, response["Content-Type"].split(";")[0]), (200, b"OK", "text/plain"))
        self.assertEqual(self.reload(self.mandate).status, MandateStatus.ACTIVE)
        self.assertEqual(self.reload(self.remita).webhook_status, "active")  # only now, with an event that named something real and that Remita confirmed

    def test_a_forged_activation_changes_nothing_because_remita_says_it_is_not_active(self):
        response = self.post(self.activation())  # the scripted Remita says the mandate is not active
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.reload(self.mandate).status, MandateStatus.PENDING_ACTIVATION)

    def test_a_notification_about_something_schoolos_does_not_know_is_acknowledged_and_changes_nothing(self):
        before = (BankTransaction.objects.count(), MandateTransaction.objects.count())
        response = self.post(self.activation(ref="NO-SUCH-MANDATE"))
        self.assertEqual((response.status_code, response.content), (200, b"OK"))
        debit = {"notificationType": "DEBIT", "lineItems": [{"mandateId": "NO-SUCH", "debitDate": "03/05/2026", "requestId": "999", "amount": "9999999"}]}
        self.post(debit)
        self.assertEqual((BankTransaction.objects.count(), MandateTransaction.objects.count()), before)
        self.assertEqual(self.reload(self.remita).webhook_status, "awaiting_event")  # nothing real arrived, so it is not called active

    def test_the_same_delivery_twice_is_processed_and_asked_about_once(self):
        self.says_active()
        self.post(self.activation())
        asked = self.server.called("POST", BASE + "/status")
        self.assertEqual(self.post(self.activation()).status_code, 200)
        self.assertEqual(self.server.called("POST", BASE + "/status"), asked)
        self.assertEqual(MandateProviderEvent.objects.count(), 1)

    def test_a_debit_notification_makes_schoolos_ask_and_only_a_confirmed_debit_is_settled(self):
        self.says_active()
        mandate_provider.refresh_mandate(self.mandate.id)
        self.server.on("POST", BASE + "/payment/send", lambda call: ok({"statuscode": "069", "RRR": "R1", "requestId": call.json["requestId"], "mandateId": call.json["mandateId"], "status": "New Transaction"}))
        batch = self.approved()
        execution.start(self.maker, batch.id)
        jobs.drain()
        item = self.item(batch, self.family)
        debit = {"notificationType": "DEBIT", "lineItems": [{"mandateId": self.mandate.provider_mandate_reference, "debitDate": "03/05/2026", "requestId": item.request_ref, "amount": "280000"}]}
        # A forged notification claiming money: Remita says the debit is still awaiting, so nothing is settled.
        self.server.on("POST", BASE + "/payment/status", ok({"statuscode": "070", "status": "Awaiting Debit", "RRR": "R1"}))
        self.post(debit)
        self.assertEqual((self.owed(self.family), MandateTransaction.objects.get().status), (280_000 * N, DebitOutcome.PENDING))
        # The real one: Remita confirms.
        self.server.on("POST", BASE + "/payment/status", ok({"statuscode": "01", "status": "Approved", "amount": "280000", "RRR": "R1"}))
        self.post({**debit, "lineItems": [{**debit["lineItems"][0], "debitDate": "04/05/2026"}]})
        self.assertEqual(self.owed(self.family), 0)
        self.assertEqual(BankTransaction.objects.filter(transaction_type="direct_debit").count(), 1)

    def test_a_status_remita_does_not_document_never_undoes_a_settled_debit(self):
        self.says_active()
        mandate_provider.refresh_mandate(self.mandate.id)
        self.server.on("POST", BASE + "/payment/send", ok({"statuscode": "01", "RRR": "R1", "status": "Approved", "amount": "280000"}))
        batch = self.run_batch(self.approved())
        item = self.item(batch, self.family)
        self.assertEqual(self.owed(self.family), 0)
        debit = {"notificationType": "DEBIT", "lineItems": [{"mandateId": self.mandate.provider_mandate_reference, "debitDate": "05/05/2026", "requestId": item.request_ref, "amount": "280000"}]}
        # Remita documents no reversal code: an unrecognised status never undoes a settled debit.
        self.server.on("POST", BASE + "/payment/status", ok({"statuscode": "999", "status": "Something new"}))
        self.post(debit)
        self.assertEqual(self.owed(self.family), 0)

    def test_when_remita_cannot_be_reached_the_provider_is_told_to_try_again_and_nothing_is_remembered_as_done(self):
        self.server.on("POST", BASE + "/status", HttpResult(503, None, ""))
        response = self.post(self.activation())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(MandateProviderEvent.objects.count(), 0)
        self.says_active()
        self.assertEqual(self.post(self.activation()).status_code, 200)  # the retry is processed
        self.assertEqual(self.reload(self.mandate).status, MandateStatus.ACTIVE)

    def test_an_unknown_address_is_a_plain_404_and_reveals_nothing(self):
        response = self.post(self.activation(), path="/api/v1/mandate-webhooks/remita/no-such-token/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.post(self.activation(), path="/api/v1/mandate-webhooks/lendsqr/no-such-token/").status_code, 404)
        self.assertEqual(self.post(self.activation(), path="/api/v1/mandate-webhooks/paystack/no-such-token/").status_code, 404)

    def test_the_address_of_one_connection_is_not_valid_for_another_provider(self):
        other = self.path.replace("/remita/", "/lendsqr/")
        self.assertEqual(self.post(self.activation(), path=other).status_code, 404)

    def test_an_unreadable_or_oversized_body_is_refused(self):
        self.assertEqual(self.post(b"not json").status_code, 400)
        self.assertEqual(self.post(b"x" * (300 * 1024)).status_code, 413)

    def test_a_disabled_connection_ignores_callbacks(self):
        from ..models import MandateProviderConnection

        MandateProviderConnection.objects.filter(pk=self.remita.pk).update(status="disabled")
        self.says_active()
        self.post(self.activation())
        self.assertEqual(self.reload(self.mandate).status, MandateStatus.PENDING_ACTIVATION)
