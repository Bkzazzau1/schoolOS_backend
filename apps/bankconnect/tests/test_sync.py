from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import override_settings

from .. import sandbox_tools, sync
from ..models import BankAuditEvent, BankConnection, BankTransaction
from ..providers.base import ConnectorError, NormalizedTransaction, SyncPage
from ..providers.sandbox import SandboxConnector
from .base import ACCOUNT, SANDBOX_KEY, BankTestCase


class SyncTests(BankTestCase):
    def setUp(self):
        super().setUp()
        connection, _ = self.connected()
        self.connection = self.row(connection)

    def feed(self, count, start=1):
        for n in range(start, start + count):
            sandbox_tools.add_feed_item(self.connection, external_transaction_id=f"E{n}", amount_minor=n * 1000)

    def test_new_transactions_are_pulled_and_the_cursor_moves_on(self):
        self.feed(3)
        outcome = sync.sync_connection(self.connection)
        self.assertEqual((outcome.ok, outcome.fetched, outcome.created, outcome.duplicates), (True, 3, 3, 0))
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_cursor["after"], self.connection.sandbox_feed.last().id)
        self.assertIsNotNone(self.connection.last_synced_at)
        again = sync.sync_connection(self.connection)
        self.assertEqual((again.fetched, again.created), (0, 0))
        self.assertEqual(BankTransaction.objects.count(), 3)

    def test_starting_over_from_the_beginning_makes_no_second_records(self):
        self.feed(3)
        sync.sync_connection(self.connection)
        self.connection.sync_cursor = {}
        self.connection.save()
        outcome = sync.sync_connection(self.connection)
        self.assertEqual((outcome.fetched, outcome.created, outcome.duplicates), (3, 0, 3))
        self.assertEqual(BankTransaction.objects.count(), 3)

    def test_a_long_backlog_is_read_a_page_at_a_time(self):
        self.feed(5)
        with mock.patch.object(sync, "PAGE_SIZE", 2):
            first = sync.sync_connection(self.connection, max_pages=2)
            self.assertEqual((first.created, first.more), (4, True))
            second = sync.sync_connection(self.connection, max_pages=2)
            self.assertEqual((second.created, second.more), (1, False))
        self.assertEqual(BankTransaction.objects.count(), 5)

    def test_a_refused_credential_marks_the_account_and_a_reconnect_resumes_where_it_left_off(self):
        self.feed(2)
        sync.sync_connection(self.connection)
        self.reseal(self.connection, {"account_number": ACCOUNT})
        self.feed(1, start=3)
        outcome = sync.sync_connection(self.connection)
        self.assertEqual((outcome.ok, outcome.error_code), (False, "bad_credentials"))
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.status, "needs_reauth")
        self.assertTrue(BankAuditEvent.objects.filter(connection=self.connection, kind="sync_failed").exists())
        response = self.api_post(
            f"connections/{self.connection.id}/reconnect/",
            {"credentials": {"sandbox_key": SANDBOX_KEY, "account_number": ACCOUNT}},
        )
        self.assertEqual(response.status_code, 200)
        self.connection.refresh_from_db()
        outcome = sync.sync_connection(self.connection)
        self.assertEqual((outcome.ok, outcome.created), (True, 1))
        self.assertEqual(BankTransaction.objects.count(), 3)

    def test_pages_that_arrived_before_a_failure_are_kept(self):
        page = SyncPage(
            [NormalizedTransaction("P1", "credit", 5000, self.connection.created_at)], {"after": 7}, has_more=True
        )
        with mock.patch.object(SandboxConnector, "fetch_transactions", side_effect=[page, ConnectorError("upstream", "The bank is busy.")]):
            outcome = sync.sync_connection(self.connection)
        self.assertEqual((outcome.ok, outcome.created, outcome.error_code), (False, 1, "upstream"))
        self.connection.refresh_from_db()
        self.assertEqual((self.connection.sync_cursor, self.connection.status), ({"after": 7}, "error"))
        self.assertIsNone(self.connection.last_synced_at)

    def test_a_credential_that_cannot_be_opened_is_reported_not_crashed_on(self):
        self.connection.sealed_credentials = b"garbage"
        self.connection.save()
        outcome = sync.sync_connection(self.connection)
        self.assertEqual((outcome.ok, outcome.error_code), (False, "vault_error"))

    def test_only_a_connected_account_syncs(self):
        for status in ("pending", "disabled", "revoked", "error", "needs_reauth"):
            self.connection.status = status
            with self.assertRaises(sync.connections.BankRejected):
                sync.sync_connection(self.connection)

    def test_invalid_items_are_counted_and_skipped(self):
        sandbox_tools.add_feed_item(self.connection, amount_minor=0)
        sandbox_tools.add_feed_item(self.connection, external_transaction_id="GOOD")
        outcome = sync.sync_connection(self.connection)
        self.assertEqual((outcome.created, outcome.invalid), (1, 1))


class SyncApiTests(BankTestCase):
    def setUp(self):
        super().setUp()
        connection, _ = self.connected()
        self.connection = self.row(connection)

    def test_the_owner_syncs_and_it_is_audited_with_counts(self):
        sandbox_tools.add_feed_item(self.connection, external_transaction_id="A")
        response = self.api_post(f"connections/{self.connection.id}/sync/")
        self.assertEqual(response.status_code, 200, response.json())
        body = response.json()
        self.assertEqual((body["sync"]["ok"], body["sync"]["created"]), (True, 1))
        self.assertIsNotNone(body["connection"]["lastSyncedAt"])
        event = BankAuditEvent.objects.get(kind="synced")
        self.assertEqual((event.detail["created"], event.actor), (1, self.owner))

    def test_the_finance_office_may_sync_without_the_duty_but_nobody_else(self):
        url = f"connections/{self.connection.id}/sync/"
        self.assertEqual(self.api_post(url, who=self.members["accountant"]).status_code, 200)
        for role in ("principal", "teacher", "parent"):
            self.assertEqual(self.api_post(url, who=self.members[role]).status_code, 403, role)

    def test_another_schools_owner_cannot_sync_ours(self):
        response = self.api_post(f"connections/{self.connection.id}/sync/", who=self.other_owner, school=self.other_school)
        self.assertEqual(response.status_code, 404)

    def test_syncing_an_account_that_is_not_connected_is_refused(self):
        self.api_post(f"connections/{self.connection.id}/disable/")
        response = self.api_post(f"connections/{self.connection.id}/sync/")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "not_live"))

    def test_a_provider_failure_is_reported_in_the_body_not_as_a_crash(self):
        self.reseal(self.connection, {"account_number": ACCOUNT})
        response = self.api_post(f"connections/{self.connection.id}/sync/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["sync"]["ok"], response.json()["sync"]["code"]), (False, "bad_credentials"))
        self.assertEqual(response.json()["connection"]["status"], "needs_reauth")


class TransactionsApiTests(BankTestCase):
    def setUp(self):
        super().setUp()
        connection, _ = self.connected()
        self.connection = self.row(connection)
        for n, (sender, narration, direction) in enumerate(
            [("Musa Ibrahim", "BG-0042 tuition", "credit"), ("Ada Obi", "term fees", "credit"), ("Bank charge", "sms alert", "debit")], start=1
        ):
            sandbox_tools.add_feed_item(
                self.connection, external_transaction_id=f"T{n}", sender_name=sender, narration=narration,
                direction=direction, amount_minor=n * 10_000, transaction_date=f"2026-09-2{n}T10:00:00+01:00",
            )
        sync.sync_connection(self.connection)

    def list(self, query="", **kw):
        return self.api_get(f"transactions/{query}", **kw)

    def test_they_come_back_newest_first_in_the_canonical_shape(self):
        body = self.list().json()
        self.assertEqual([t["amountMinor"] for t in body["transactions"]], [30_000, 20_000, 10_000])
        first = body["transactions"][2]
        self.assertEqual((first["senderName"], first["direction"], first["maskedAccountNumber"], first["isSandbox"]), ("Musa Ibrahim", "credit", "****6789", True))
        self.assertEqual((body["total"], body["hasMore"]), (3, False))
        self.assert_no_secrets(body)

    def test_filters_narrow_the_list(self):
        self.assertEqual(self.list("?direction=debit").json()["total"], 1)
        self.assertEqual(self.list("?status=not_applicable").json()["total"], 1)
        self.assertEqual(self.list("?status=unmatched").json()["total"], 2)
        self.assertEqual(self.list("?q=musa").json()["total"], 1)
        self.assertEqual(self.list("?q=TERM").json()["total"], 1)
        self.assertEqual(self.list("?from=2026-09-22&to=2026-09-22").json()["total"], 1)
        self.assertEqual(self.list(f"?connection={self.connection.id}").json()["total"], 3)

    def test_paging(self):
        page = self.list("?limit=2").json()
        self.assertEqual((len(page["transactions"]), page["hasMore"], page["total"]), (2, True, 3))
        self.assertEqual(len(self.list("?limit=2&offset=2").json()["transactions"]), 1)

    def test_bad_filters_are_refused(self):
        for query in ("?status=bogus", "?direction=sideways", "?from=yesterday", "?connection=abc", "?limit=lots"):
            response = self.list(query)
            self.assertEqual((response.status_code, response.json()["code"]), (400, "invalid_filter"), query)

    def test_the_finance_office_reads_them_and_nobody_else_does(self):
        self.assertEqual(self.list(who=self.members["accountant"]).status_code, 200)
        for role in ("principal", "administrator", "teacher", "staff", "parent", "driver"):
            self.assertEqual(self.list(who=self.members[role]).status_code, 403, role)

    def test_another_school_sees_none_of_them(self):
        theirs = self.list(who=self.other_owner, school=self.other_school).json()
        self.assertEqual(theirs["total"], 0)
        filtered = self.list(f"?connection={self.connection.id}", who=self.other_owner, school=self.other_school).json()
        self.assertEqual(filtered["total"], 0)
        self.assertEqual(self.list(who=self.other_owner).status_code, 403)


class SyncCommandTests(BankTestCase):
    def run_command(self, *args):
        out, err = StringIO(), StringIO()
        try:
            call_command("sync_bank_connections", *args, stdout=out, stderr=err)
        finally:
            self.output, self.errors = out.getvalue(), err.getvalue()

    def test_it_syncs_every_connected_account_at_every_school_and_skips_the_rest(self):
        ours = self.row(self.connected()[0])
        theirs = self.row(self.connected(who=self.other_owner, school=self.other_school)[0])
        paused = self.row(self.connected(account="0123456780")[0])
        self.api_post(f"connections/{paused.id}/disable/")
        for connection in (ours, theirs, paused):
            sandbox_tools.add_feed_item(connection)
        self.run_command()
        self.assertIn("connections=2 new_transactions=2 failed=0", self.output)
        self.assertEqual(BankTransaction.objects.filter(connection=paused).count(), 0)
        self.run_command()
        self.assertIn("new_transactions=0", self.output)

    def test_one_broken_account_does_not_stop_the_others_and_the_run_reports_failure(self):
        good = self.row(self.connected()[0])
        broken = self.row(self.connected(who=self.other_owner, school=self.other_school)[0])
        self.reseal(broken, {"account_number": ACCOUNT})
        sandbox_tools.add_feed_item(good)
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual(BankTransaction.objects.filter(connection=good).count(), 1)
        self.assertIn(f"{broken.id}: bad_credentials", self.errors)
        self.assertNotIn(SANDBOX_KEY, self.errors + self.output)

    def test_it_can_be_limited_to_one_school(self):
        ours = self.row(self.connected()[0])
        theirs = self.row(self.connected(who=self.other_owner, school=self.other_school)[0])
        for connection in (ours, theirs):
            sandbox_tools.add_feed_item(connection)
        self.run_command("--school", self.school.slug)
        self.assertEqual(BankTransaction.objects.filter(connection=theirs).count(), 0)
        self.assertEqual(BankTransaction.objects.filter(connection=ours).count(), 1)


class SandboxPostCommandTests(BankTestCase):
    def test_it_puts_a_credit_on_a_sandbox_connection_and_syncs_it(self):
        connection = self.row(self.connected()[0])
        out = StringIO()
        call_command("bankconnect_sandbox_post", str(connection.id), "--naira", "1500", "--sender", "Musa", "--narration", "BG-1", stdout=out)
        row = BankTransaction.objects.get()
        self.assertEqual((row.amount_minor, row.sender_name, row.narration), (150_000, "Musa", "BG-1"))

    def test_it_refuses_a_real_connection(self):
        real = BankConnection.objects.create(school=self.school, provider="gtbank", connection_type="direct_bank_api", is_sandbox=False)
        with self.assertRaises(CommandError):
            call_command("bankconnect_sandbox_post", str(real.id), "--naira", "10", stdout=StringIO())

    @override_settings(BANKCONNECT_ENABLE_SANDBOX=False)
    def test_it_refuses_when_the_sandbox_is_off(self):
        with self.assertRaises(CommandError):
            call_command("bankconnect_sandbox_post", "x", "--naira", "10", stdout=StringIO())
