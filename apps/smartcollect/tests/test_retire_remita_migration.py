"""Remita was briefly treated as a Smart Money Collection provider (an RRR standing in for a family's account). It is not one: it is
reserved for a separate Mandates / Direct Debit feature. These tests run the real migrations over rows the earlier design left behind and
prove that everything Remita is taken out of Smart Money Collection, nothing is deleted, and nothing of Paystack's or Monnify's is touched."""

from datetime import date

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

BEFORE = [
    ("bankconnect", "0004_collection_provider_connection"),
    ("receivables", "0007_collection_account_reuse_and_grace"),
    ("smartcollect", "0001_initial"),
]
WHY = "Remita is not a Smart Money Collection provider. It is reserved for Mandates / Direct Debit."


class RetireRemitaMigrationTests(TransactionTestCase):
    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(BEFORE)
        self.addCleanup(self.migrate_to_latest)
        self.old = executor.loader.project_state(BEFORE).apps
        self.build_what_the_earlier_design_left()

    def migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def now_apps(self):
        executor = MigrationExecutor(connection)
        return executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps

    # -- what the earlier design could have left ----------------------------------------------------

    def build_what_the_earlier_design_left(self):
        old = self.old
        School, Session = old.get_model("schools", "School"), old.get_model("academics", "AcademicSession")
        Family = old.get_model("receivables", "Family")
        Connection = old.get_model("bankconnect", "CollectionProviderConnection")
        Account = old.get_model("receivables", "FamilyCollectionAccount")
        Batch, Item = old.get_model("smartcollect", "CollectionGenerationBatch"), old.get_model("smartcollect", "CollectionGenerationBatchItem")
        Job, Switch = old.get_model("smartcollect", "ProviderJob"), old.get_model("smartcollect", "ProviderSwitch")

        self.school = School.objects.create(name="BrightGate", slug="brightgate")
        self.other = School.objects.create(name="Other", slug="other")
        session = Session.objects.create(school=self.school, code="2026/27", name="2026/2027", starts_on=date(2026, 9, 7), ends_on=date(2027, 7, 20), status="active")
        fam = {n: Family.objects.create(school=self.school, code=n, display_name=f"Family {n}") for n in ("F1", "F2", "F3")}

        common = {"connection_type": "collection_provider", "environment": "test"}
        self.remita = Connection.objects.create(
            school=self.school, provider="remita", status="connected", is_active_provider=True, sealed_credentials=b"sealed-remita",
            webhook_token_hash="hash-of-token", webhook_status="active", **common,
        )
        self.paystack = Connection.objects.create(
            school=self.school, provider="paystack", status="connected", sealed_credentials=b"sealed-paystack", webhook_token_hash="hash-2", webhook_status="active", **common,
        )
        self.gone = Connection.objects.create(school=self.other, provider="remita", status="revoked", sealed_credentials=b"leftover", **common)

        def account(family, provider, connection_row, **kw):
            return Account.objects.create(school=self.school, family=family, provider=provider, connection=connection_row, origin="provider", **kw)

        self.rrr = account(fam["F1"], "remita", self.remita, status="active", account_number="RRR-1", external_account_ref="RRR-1")
        self.no_rrr = account(fam["F2"], "remita", self.remita, status="provisioning")
        self.kept_account = account(fam["F3"], "paystack", self.paystack, status="active", account_number="9930000001", external_account_ref="ACCT_1")

        def batch(provider, connection_row, status, **kw):
            return Batch.objects.create(school=self.school, session=session, provider_connection=connection_row, provider=provider, environment="test", status=status, **kw)

        self.draft = batch("remita", self.remita, "draft")
        self.draft_item = Item.objects.create(batch=self.draft, school=self.school, family=fam["F1"], selected=True, generation_status="pending")
        self.running = batch("remita", self.remita, "processing")
        self.made = Item.objects.create(
            batch=self.running, school=self.school, family=fam["F1"], selected=True, generation_status="success", provider_account_reference="RRR-1",
        )
        self.waiting = Item.objects.create(batch=self.running, school=self.school, family=fam["F2"], selected=True, generation_status="pending")
        self.done = batch("remita", self.remita, "completed")
        self.paystack_batch = batch("paystack", self.paystack, "draft")
        self.paystack_item = Item.objects.create(batch=self.paystack_batch, school=self.school, family=fam["F3"], selected=True, generation_status="pending")

        soon = timezone.now()
        self.remita_job = Job.objects.create(school=self.school, kind="provision", status="queued", item=self.waiting, idempotency_key="job-remita", run_after=soon)
        self.paystack_job = Job.objects.create(school=self.school, kind="provision", status="queued", item=self.paystack_item, idempotency_key="job-paystack", run_after=soon)
        self.switch = Switch.objects.create(school=self.school, from_connection=self.remita, to_connection=self.paystack, status="scheduled", scheduled_for=soon)

    # -- the migration --------------------------------------------------------------------------

    def test_the_migrations_take_remita_out_of_smart_money_collection_and_delete_nothing(self):
        self.migrate_to_latest()  # run once: each run migrates the whole project back and forth
        apps = self.now_apps()
        for check in (
            self.connections_are_disconnected, self.accounts_are_closed, self.batches_are_settled, self.jobs_and_switches_are_ended,
            self.every_change_is_audited, self.the_database_refuses_remita_as_active,
        ):
            with self.subTest(check.__name__):
                check(apps)

    def connections_are_disconnected(self, apps):
        Connection = apps.get_model("bankconnect", "CollectionProviderConnection")
        remita = Connection.objects.get(pk=self.remita.pk)
        self.assertEqual((remita.status, remita.is_active_provider, remita.last_error_code), ("revoked", False, "provider_removed"))
        self.assertEqual((bytes(remita.sealed_credentials), remita.webhook_token_hash, remita.webhook_status), (b"", "", "not_configured"))
        self.assertIsNotNone(remita.disconnected_at)
        self.assertEqual(bytes(Connection.objects.get(pk=self.gone.pk).sealed_credentials), b"")  # even an already disconnected row is wiped
        paystack = Connection.objects.get(pk=self.paystack.pk)
        self.assertEqual((paystack.status, paystack.is_active_provider, bytes(paystack.sealed_credentials)), ("connected", False, b"sealed-paystack"))
        self.assertEqual(Connection.objects.filter(is_active_provider=True).count(), 0)  # the school chooses; nothing is switched on for it

    def accounts_are_closed(self, apps):
        Account = apps.get_model("receivables", "FamilyCollectionAccount")
        rrr, no_rrr = Account.objects.get(pk=self.rrr.pk), Account.objects.get(pk=self.no_rrr.pk)
        self.assertEqual((rrr.status, rrr.close_reason, rrr.account_number, rrr.external_account_ref), ("closed", WHY, "RRR-1", "RRR-1"))
        self.assertIsNotNone(rrr.closed_at)
        self.assertEqual(no_rrr.status, "failed")  # it never had a number, so it never received anything
        self.assertEqual(Account.objects.get(pk=self.kept_account.pk).status, "active")
        self.assertEqual(Account.objects.count(), 3)  # nothing deleted

    def batches_are_settled(self, apps):
        Batch, Item = apps.get_model("smartcollect", "CollectionGenerationBatch"), apps.get_model("smartcollect", "CollectionGenerationBatchItem")
        draft, running = Batch.objects.get(pk=self.draft.pk), Batch.objects.get(pk=self.running.pk)
        self.assertEqual(draft.status, "cancelled")
        self.assertEqual(Item.objects.get(pk=self.draft_item.pk).generation_status, "skipped")
        self.assertEqual((running.status, running.success_count, running.failed_count), ("partially_successful", 1, 1))
        self.assertEqual(Item.objects.get(pk=self.made.pk).generation_status, "success")  # the family that succeeded keeps its account
        stopped = Item.objects.get(pk=self.waiting.pk)
        self.assertEqual((stopped.generation_status, stopped.error_code), ("failed", "provider_removed"))
        self.assertEqual(Batch.objects.get(pk=self.done.pk).status, "completed")
        self.assertEqual(Batch.objects.get(pk=self.paystack_batch.pk).status, "draft")
        self.assertEqual(Item.objects.get(pk=self.paystack_item.pk).generation_status, "pending")

    def jobs_and_switches_are_ended(self, apps):
        Job, Switch = apps.get_model("smartcollect", "ProviderJob"), apps.get_model("smartcollect", "ProviderSwitch")
        failed = Job.objects.get(pk=self.remita_job.pk)
        self.assertEqual((failed.status, failed.last_error_code), ("failed", "provider_removed"))
        self.assertEqual(Job.objects.get(pk=self.paystack_job.pk).status, "queued")
        switch = Switch.objects.get(pk=self.switch.pk)
        self.assertEqual((switch.status, switch.cancel_reason), ("cancelled", WHY))

    def every_change_is_audited(self, apps):
        Audit, Bank = apps.get_model("smartcollect", "CollectionAuditEvent"), apps.get_model("bankconnect", "BankAuditEvent")
        kinds = sorted(Audit.objects.values_list("kind", flat=True))
        self.assertEqual(
            kinds,
            sorted([
                "provider_switch_cancelled", "batch_cancelled_provider_removed", "batch_stopped_provider_removed",
                "collection_account_retired_provider_removed", "collection_account_retired_provider_removed",
            ]),
        )
        self.assertEqual(Bank.objects.filter(kind="provider_retired").count(), 2)  # both Remita connections, the disconnected one included
        BatchEvent = apps.get_model("smartcollect", "CollectionBatchEvent")
        self.assertEqual(sorted(BatchEvent.objects.values_list("kind", flat=True)), ["cancelled", "stopped"])

    def the_database_refuses_remita_as_active(self, apps):
        Connection = apps.get_model("bankconnect", "CollectionProviderConnection")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Connection.objects.create(school_id=self.other.pk, provider="remita", status="connected", is_active_provider=True)
        Connection.objects.create(school_id=self.other.pk, provider="paystack", status="connected", is_active_provider=True)  # a real provider may be
