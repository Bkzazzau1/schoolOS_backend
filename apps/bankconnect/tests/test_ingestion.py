from datetime import datetime, timezone
from unittest import mock

from django.db import IntegrityError, transaction

from ..ingestion import CREATED, DUPLICATE, INVALID, ingest
from ..models import BankTransaction
from ..providers.base import NormalizedTransaction
from .base import BankTestCase


def tx(**over) -> NormalizedTransaction:
    fields = dict(
        external_transaction_id="EXT-1", direction="credit", amount_minor=250_000,
        transaction_date=datetime(2026, 9, 25, 10, 30, tzinfo=timezone.utc), sender_name="Musa Ibrahim",
        sender_account_number="3456789012", sender_bank="Zenith", narration="BG-0042 tuition",
        transaction_reference="REF-9", provider_session_id="SESS-1", raw_provider_reference="RAW-1",
    )
    fields.update(over)
    return NormalizedTransaction(**fields)


class IngestTests(BankTestCase):
    def setUp(self):
        super().setUp()
        connection, _ = self.connected()
        self.connection = self.row(connection)

    def test_a_credit_is_stored_in_the_canonical_shape(self):
        result = ingest(self.connection, tx())
        self.assertEqual(result.outcome, CREATED)
        row = result.transaction
        self.assertEqual(
            (row.school, row.connection, row.provider, row.bank_name, row.masked_account_number, row.is_sandbox),
            (self.school, self.connection, "sandbox", "Sandbox Bank", "****6789", True),
        )
        self.assertEqual(
            (row.direction, row.amount_minor, row.currency, row.sender_name, row.sender_bank, row.narration),
            ("credit", 250_000, "NGN", "Musa Ibrahim", "Zenith", "BG-0042 tuition"),
        )
        self.assertEqual(row.reconciliation_status, "unmatched")

    def test_the_senders_full_account_number_is_never_stored(self):
        row = ingest(self.connection, tx()).transaction
        self.assertEqual(row.sender_account_mask, "****9012")
        every_value = " ".join(str(getattr(row, f.name)) for f in BankTransaction._meta.fields)
        self.assertNotIn("3456789012", every_value)

    def test_money_going_out_is_kept_but_not_reconciled(self):
        row = ingest(self.connection, tx(direction="debit")).transaction
        self.assertEqual(row.reconciliation_status, "not_applicable")

    def test_the_same_transaction_twice_is_one_record(self):
        first = ingest(self.connection, tx())
        again = ingest(self.connection, tx(narration="changed on redelivery"))
        self.assertEqual((again.outcome, again.transaction.id), (DUPLICATE, first.transaction.id))
        self.assertEqual(BankTransaction.objects.count(), 1)
        self.assertEqual(BankTransaction.objects.get().narration, "BG-0042 tuition")

    def test_the_same_provider_session_under_another_id_is_still_one_record(self):
        first = ingest(self.connection, tx())
        again = ingest(self.connection, tx(external_transaction_id="EXT-OTHER"))
        self.assertEqual((again.outcome, again.transaction.id), (DUPLICATE, first.transaction.id))
        self.assertEqual(BankTransaction.objects.count(), 1)

    def test_a_repeat_does_not_spoil_the_callers_transaction(self):
        ingest(self.connection, tx())
        with transaction.atomic():
            self.assertEqual(ingest(self.connection, tx()).outcome, DUPLICATE)
            self.assertEqual(ingest(self.connection, tx(external_transaction_id="EXT-2", provider_session_id="S2")).outcome, CREATED)
        self.assertEqual(BankTransaction.objects.count(), 2)

    def test_two_transactions_without_a_session_id_do_not_collide(self):
        ingest(self.connection, tx(provider_session_id=""))
        self.assertEqual(ingest(self.connection, tx(external_transaction_id="EXT-2", provider_session_id="")).outcome, CREATED)

    def test_the_same_external_id_at_another_school_is_its_own_record(self):
        other, _ = self.connected(who=self.other_owner, school=self.other_school)
        ingest(self.connection, tx())
        self.assertEqual(ingest(self.row(other), tx()).outcome, CREATED)
        self.assertEqual(BankTransaction.objects.filter(school=self.school).count(), 1)
        self.assertEqual(BankTransaction.objects.filter(school=self.other_school).count(), 1)

    def test_malformed_transactions_are_refused_and_nothing_is_stored(self):
        for bad in (
            tx(amount_minor=0), tx(amount_minor=-5), tx(amount_minor=10.5), tx(amount_minor=True),
            tx(direction="sideways"), tx(external_transaction_id="   "), tx(currency="NAIRA"),
            tx(transaction_date="2026-09-25"),
        ):
            self.assertEqual(ingest(self.connection, bad).outcome, INVALID, bad)
        self.assertEqual(BankTransaction.objects.count(), 0)

    def test_text_is_tidied_and_cut_to_what_the_columns_hold(self):
        row = ingest(self.connection, tx(narration="a  \n  b " + "x" * 900, sender_name="  Musa   Ibrahim ")).transaction
        self.assertEqual(row.sender_name, "Musa Ibrahim")
        self.assertTrue(row.narration.startswith("a b xxx"))
        self.assertEqual(len(row.narration), 500)

    def test_a_time_without_a_zone_is_taken_as_utc(self):
        row = ingest(self.connection, tx(transaction_date=datetime(2026, 9, 25, 10, 30))).transaction
        self.assertEqual(row.transaction_date, datetime(2026, 9, 25, 10, 30, tzinfo=timezone.utc))

    def test_a_failure_that_is_not_a_repeat_is_not_hidden(self):
        with mock.patch.object(BankTransaction.objects, "create", side_effect=IntegrityError("something else")):
            with self.assertRaises(IntegrityError):
                ingest(self.connection, tx())
