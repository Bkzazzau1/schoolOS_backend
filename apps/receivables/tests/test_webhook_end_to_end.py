from cryptography.fernet import Fernet
from django.test import override_settings

from apps.bankconnect import sandbox_tools, sync
from apps.bankconnect.identifiers import hash_token
from apps.bankconnect.models import BankConnection, BankTransaction, BankWebhookEvent, TransactionAllocation
from apps.bankconnect.providers.sandbox import SIGNATURE_HEADER, SandboxConnector
from apps.bankconnect.vault import context_for, get_vault

from .. import collection_accounts, ledger
from .base import ReceivablesTestCase

N = 100
TOKEN = "a-long-random-webhook-token-for-the-test"
SECRET = "webhook-signing-secret"
ACCOUNT = "SBX-FAMILY-0001"


@override_settings(BANKCONNECT_SECRET_KEYS=[Fernet.generate_key().decode()], BANKCONNECT_ENABLE_SANDBOX=True)
class FromTheProvidersCallbackToTheLedgerTests(ReceivablesTestCase):
    """The public route a provider calls, all the way to a family's charges - and what a repeat does."""

    def setUp(self):
        super().setUp()
        self.bello_family()
        self.publish_bello_fees()
        self.connection = BankConnection.objects.create(
            school=self.school, provider="sandbox", connection_type="sandbox", bank_name="Sandbox Bank", account_name="SCHOOL",
            account_mask="****6789", purpose="tuition", status="connected", is_sandbox=True, webhook_token_hash=hash_token(TOKEN),
        )
        self.connection.sealed_credentials = get_vault().seal(
            context_for(self.connection), {"sandbox_key": "sandbox-x", "account_number": "0123456789", "webhook_secret": SECRET}
        )
        self.connection.save()
        collection_accounts.register(self.family, provider="sandbox", account_number=ACCOUNT, actor=self.owner)
        self.client.force_authenticate(None)  # a provider has no sign-in

    def deliver(self, **payload):
        import json

        body = json.dumps({"transaction": sandbox_tools.transaction_payload(**payload)}).encode()
        signature = SandboxConnector.sign(body, SECRET)
        return self.client.post(
            f"/api/v1/bank-webhooks/sandbox/{TOKEN}/", data=body, content_type="application/json",
            **{"HTTP_" + SIGNATURE_HEADER.upper().replace("-", "_"): signature},
        ), body

    def paid(self, tx):
        return sum(a.amount_minor for a in TransactionAllocation.objects.filter(transaction=tx, receivable__isnull=False, superseded=False))

    def test_a_delivery_into_a_family_account_pays_that_familys_charges(self):
        response, _ = self.deliver(external_transaction_id="W1", amount_minor=120_000 * N, receiving_account_reference=ACCOUNT, sender_name="Somebody")
        self.assertEqual((response.status_code, response.json()["outcome"]), (200, "processed"))
        tx = BankTransaction.objects.get()
        self.assertEqual((tx.reconciliation_status, tx.family, tx.receiving_account_ref), ("matched", self.family, ACCOUNT))
        self.assertEqual(self.paid(tx), 120_000 * N)
        self.assertEqual(ledger.family_position(self.family).outstanding, 180_000 * N)
        self.assertLedgerHolds()

    def test_the_same_delivery_again_changes_nothing(self):
        _, body = self.deliver(external_transaction_id="W1", amount_minor=120_000 * N, receiving_account_reference=ACCOUNT)
        signature = SandboxConnector.sign(body, SECRET)
        for _ in range(3):
            again = self.client.post(f"/api/v1/bank-webhooks/sandbox/{TOKEN}/", data=body, content_type="application/json", **{"HTTP_X_SANDBOX_SIGNATURE": signature})
            self.assertEqual(again.json()["outcome"], "duplicate")
        self.assertEqual((BankTransaction.objects.count(), BankWebhookEvent.objects.count()), (1, 1))
        self.assertEqual(self.paid(BankTransaction.objects.get()), 120_000 * N)
        self.assertEqual(ledger.family_position(self.family).outstanding, 180_000 * N)

    def test_a_redelivery_with_the_same_transaction_id_is_one_payment_even_if_the_body_differs(self):
        self.deliver(external_transaction_id="W1", amount_minor=120_000 * N, receiving_account_reference=ACCOUNT, narration="first")
        response, _ = self.deliver(external_transaction_id="W1", amount_minor=120_000 * N, receiving_account_reference=ACCOUNT, narration="second")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(BankTransaction.objects.count(), 1)
        self.assertEqual(ledger.family_position(self.family).outstanding, 180_000 * N)

    def test_the_same_payment_seen_by_a_sync_afterwards_is_not_paid_twice(self):
        self.deliver(external_transaction_id="W1", amount_minor=120_000 * N, receiving_account_reference=ACCOUNT)
        rows_before = self.allocation_rows()
        self.assertGreater(rows_before, 0)
        sandbox_tools.add_feed_item(self.connection, external_transaction_id="W1", amount_minor=120_000 * N, receiving_account_reference=ACCOUNT)
        outcome = sync.sync_connection(self.connection)
        self.assertEqual((outcome.created, outcome.duplicates), (0, 1))
        self.assertEqual(ledger.family_position(self.family).outstanding, 180_000 * N)
        self.assertEqual(self.allocation_rows(), rows_before)  # not one more row

    def allocation_rows(self):
        return TransactionAllocation.objects.filter(receivable__isnull=False, superseded=False).count()

    def test_a_forged_delivery_pays_nothing(self):
        import json

        body = json.dumps({"transaction": sandbox_tools.transaction_payload(external_transaction_id="EVIL", amount_minor=999_999 * N, receiving_account_reference=ACCOUNT)}).encode()
        response = self.client.post(f"/api/v1/bank-webhooks/sandbox/{TOKEN}/", data=body, content_type="application/json", HTTP_X_SANDBOX_SIGNATURE="0" * 64)
        self.assertEqual(response.status_code, 400)
        self.assertEqual((BankTransaction.objects.count(), TransactionAllocation.objects.count()), (0, 0))
        self.assertEqual(ledger.family_position(self.family).outstanding, 300_000 * N)

    def test_a_delivery_naming_an_account_that_is_no_familys_is_not_guessed_at(self):
        self.deliver(external_transaction_id="W2", amount_minor=50_000 * N, receiving_account_reference="SOMEONE-ELSES-ACCOUNT", sender_name="Nobody", narration="hello")
        tx = BankTransaction.objects.get()
        self.assertEqual((tx.family, tx.reconciliation_status), (None, "unmatched"))
        self.assertEqual(ledger.family_position(self.family).outstanding, 300_000 * N)

    def test_payments_that_arrive_together_each_pay_once_and_the_ledger_holds(self):
        for n, amount in enumerate((50_000, 70_000, 200_000), start=1):
            self.deliver(external_transaction_id=f"W{n}", amount_minor=amount * N, receiving_account_reference=ACCOUNT, sender_name=f"Payer {n}")
        pos = ledger.family_position(self.family)
        self.assertEqual((pos.outstanding, pos.credit, pos.paid), (0, 20_000 * N, 300_000 * N))
        self.assertEqual(BankTransaction.objects.count(), 3)
        self.assertLedgerHolds()
