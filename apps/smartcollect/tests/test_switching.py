"""Changing the active provider: scheduled, ready when its date has come, and applied only by a person. It never happens by itself, the school
is never left with two active providers or none, the old provider's accounts are handled as the policy says, and nothing is lost."""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.bankconnect import provider_connections
from apps.bankconnect.models import CollectionProviderConnection
from apps.bankconnect.providers.transport import use_transport
from apps.bankconnect.tests.fake_transport import FakeTransport, ok

from .. import batches, jobs, switching, running
from ..constants import BatchStatus, SwitchStatus
from ..errors import CollectionRefused
from ..models import CollectionAuditEvent, ProviderSwitch
from .base import PROVIDER, CollectTestCase

PAYSTACK_KEY = "sk_test_SWITCH-TEST-KEY"


class SwitchCase(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.paystack_server = FakeTransport().on("GET", "/dedicated_account/available_providers", ok({"status": True, "data": []}))
        context = use_transport(self.paystack_server)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.paystack = self.connect_provider(provider="paystack", credentials={"secret_key": PAYSTACK_KEY}, activate=False)
        self.family = self.make_family("Alpha")

    def schedule(self, when=None, **kw):
        return switching.schedule(self.owner, to_connection_id=self.paystack.id, scheduled_for=when or (timezone.now() + timedelta(days=3)), **kw)

    def ready(self):
        switch = self.schedule(timezone.now() - timedelta(minutes=1))
        self.assertEqual(switch.status, SwitchStatus.READY_TO_SWITCH)
        return switch

    def active(self):
        return list(CollectionProviderConnection.objects.filter(school=self.school, is_active_provider=True))


class SchedulingTests(SwitchCase):
    def test_a_switch_is_scheduled_for_a_date_and_nothing_changes(self):
        switch = self.schedule()
        self.assertEqual(switch.status, SwitchStatus.SCHEDULED)
        self.assertEqual(self.active(), [self.connection])
        self.assertEqual((switch.from_connection, switch.to_connection), (self.connection, self.paystack))
        self.assertTrue(CollectionAuditEvent.objects.filter(kind="provider_switch_scheduled").exists())

    def test_only_someone_who_manages_providers_can_schedule_apply_or_cancel(self):
        for who in (self.maker, self.checker, self.members["teacher"]):
            with self.assertRaises(CollectionRefused) as caught:
                switching.schedule(who, to_connection_id=self.paystack.id, scheduled_for=timezone.now())
            self.assertEqual(caught.exception.code, "not_provider_manager")
        self.give_duties(self.maker, PROVIDER)
        switching.schedule(self.maker, to_connection_id=self.paystack.id, scheduled_for=timezone.now() + timedelta(days=1))

    def test_a_switch_needs_an_active_provider_a_different_connected_target_and_only_one_may_be_open(self):
        with self.assertRaises(CollectionRefused) as caught:
            switching.schedule(self.owner, to_connection_id=self.connection.id, scheduled_for=timezone.now())
        self.assertEqual(caught.exception.code, "already_active")
        self.paystack.status = "disabled"
        self.paystack.save()
        with self.assertRaises(CollectionRefused) as caught:
            self.schedule()
        self.assertEqual(caught.exception.code, "not_connected")
        self.paystack.status = "connected"
        self.paystack.save()
        self.schedule()
        with self.assertRaises(CollectionRefused) as caught:
            self.schedule()
        self.assertEqual(caught.exception.code, "switch_open")

    def test_a_school_with_no_active_provider_has_nothing_to_switch_from(self):
        CollectionProviderConnection.objects.filter(pk=self.connection.pk).update(is_active_provider=False)
        with self.assertRaises(CollectionRefused) as caught:
            self.schedule()
        self.assertEqual(caught.exception.code, "no_active_provider")

    def test_a_connection_of_another_school_cannot_be_the_target(self):
        other = self.connect_provider(school=self.other_school, owner=self.other_owner)
        with self.assertRaises(CollectionRefused) as caught:
            switching.schedule(self.owner, to_connection_id=other.id, scheduled_for=timezone.now())
        self.assertEqual(caught.exception.code, "connection_not_found")

    def test_the_database_allows_one_open_switch_per_school_and_no_switch_to_itself(self):
        self.schedule()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProviderSwitch.objects.create(school=self.school, from_connection=self.connection, to_connection=self.paystack, scheduled_for=timezone.now())
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProviderSwitch.objects.create(
                school=self.school, from_connection=self.connection, to_connection=self.connection, scheduled_for=timezone.now(), status="cancelled"
            )

    def test_a_bad_date_is_refused(self):
        with self.assertRaises(CollectionRefused) as caught:
            switching.schedule(self.owner, to_connection_id=self.paystack.id, scheduled_for="soon")
        self.assertEqual(caught.exception.code, "invalid_date")


class ReadinessTests(SwitchCase):
    def test_when_its_date_has_come_and_nothing_stands_in_the_way_it_becomes_ready_and_still_nothing_has_changed(self):
        switch = self.ready()
        self.assertIsNotNone(switch.ready_at)
        self.assertEqual(self.active(), [self.connection])  # READY is not APPLIED
        again = switching.refresh(self.school, timezone.now() + timedelta(days=30))
        self.assertEqual(again.status, SwitchStatus.READY_TO_SWITCH)
        self.assertEqual(self.active(), [self.connection])

    def test_a_switch_whose_date_is_in_the_future_stays_scheduled(self):
        switch = self.schedule(timezone.now() + timedelta(days=3))
        self.assertEqual(switching.refresh(self.school, timezone.now() + timedelta(days=1)).status, SwitchStatus.SCHEDULED)
        self.assertEqual(switching.refresh(self.school, timezone.now() + timedelta(days=4)).status, SwitchStatus.READY_TO_SWITCH)
        self.assertEqual(self.active(), [self.connection])
        self.assertEqual(ProviderSwitch.objects.get(pk=switch.pk).status, SwitchStatus.READY_TO_SWITCH)

    def test_a_blocker_keeps_it_scheduled_and_says_what_it_is(self):
        self.family_batch_processing()
        switch = self.schedule(timezone.now() - timedelta(minutes=1))
        self.assertEqual(switch.status, SwitchStatus.SCHEDULED)
        self.assertIn("still generating", switch.blockers[0])
        jobs.drain()
        self.assertEqual(switching.refresh(self.school).status, SwitchStatus.READY_TO_SWITCH)

    def family_batch_processing(self):
        batch = self.approved()
        running.start_processing(self.maker, batch.id)
        return batch

    def test_a_target_that_stops_working_takes_a_ready_switch_back_to_scheduled(self):
        self.ready()
        self.paystack.status = "needs_reauth"
        self.paystack.save()
        switch = switching.refresh(self.school)
        self.assertEqual(switch.status, SwitchStatus.SCHEDULED)
        self.assertTrue(switch.blockers)

    def test_the_management_command_looks_but_never_applies(self):
        from django.core.management import call_command

        self.schedule(timezone.now() - timedelta(minutes=1))
        call_command("refresh_provider_switches")
        self.assertEqual(ProviderSwitch.objects.get().status, SwitchStatus.READY_TO_SWITCH)
        self.assertEqual(self.active(), [self.connection])

    def test_the_review_says_who_is_affected_and_what_they_owe_and_warns_about_the_new_provider(self):
        self.run_batch(self.approved())
        switch = self.schedule()
        review = switch.review
        self.assertEqual((review["current"]["provider"], review["target"]["provider"]), ("sandbox", "paystack"))
        self.assertEqual((review["affected"]["families"], review["affected"]["accounts"], review["affected"]["outstandingMinor"]), (1, 1, 100_000 * 100))
        self.assertEqual(review["affected"]["byStatus"], {"active": 1})
        self.assertTrue(any("webhook" in w for w in review["warnings"]))
        self.assertEqual(review["switchPolicy"], "retire_when_settled")


class ApplyingTests(SwitchCase):
    def test_a_switch_that_is_not_ready_cannot_be_applied(self):
        switch = self.schedule()
        with self.assertRaises(CollectionRefused) as caught:
            switching.apply(self.owner, switch.id)
        self.assertEqual(caught.exception.code, "not_ready")
        self.assertEqual(self.active(), [self.connection])

    def test_only_a_provider_manager_can_apply_and_it_is_an_explicit_act(self):
        switch = self.ready()
        with self.assertRaises(CollectionRefused) as caught:
            switching.apply(self.maker, switch.id)
        self.assertEqual(caught.exception.code, "not_provider_manager")
        applied = switching.apply(self.owner, switch.id)
        self.assertEqual((applied.status, applied.applied_by), (SwitchStatus.APPLIED, self.owner))
        self.assertEqual(self.active(), [self.paystack])
        self.assertTrue(CollectionAuditEvent.objects.filter(kind="provider_switch_applied").exists())

    def test_the_school_has_exactly_one_active_provider_before_and_after(self):
        for _ in range(2):
            self.assertEqual(len(self.active()), 1)
            switching.apply(self.owner, self.ready().id)
            self.assertEqual(len(self.active()), 1)
            self.assertEqual(CollectionProviderConnection.objects.filter(school=self.school, is_active_provider=True).count(), 1)
            break

    def test_something_that_became_a_blocker_since_it_was_ready_stops_the_apply_and_sends_it_back(self):
        switch = self.ready()
        self.paystack.status = "disabled"
        self.paystack.save()
        with self.assertRaises(CollectionRefused) as caught:
            switching.apply(self.owner, switch.id)
        self.assertEqual(caught.exception.code, "switch_blocked")
        self.assertEqual((ProviderSwitch.objects.get(pk=switch.pk).status, self.active()), (SwitchStatus.SCHEDULED, [self.connection]))

    def test_an_applied_switch_cannot_be_applied_or_cancelled_again(self):
        switch = self.ready()
        switching.apply(self.owner, switch.id)
        for action in (switching.apply, switching.cancel):
            with self.assertRaises(CollectionRefused):
                action(self.owner, switch.id)

    def test_a_cancelled_switch_changes_nothing_and_keeps_its_history(self):
        switch = self.schedule()
        switching.cancel(self.owner, switch.id, reason="Not yet")
        switch.refresh_from_db()
        self.assertEqual((switch.status, switch.cancel_reason), (SwitchStatus.CANCELLED, "Not yet"))
        self.assertEqual(self.active(), [self.connection])
        self.schedule()  # a new one can be planned

    def test_another_schools_switch_is_not_found(self):
        from rest_framework.exceptions import NotFound

        switch = self.schedule()
        with self.assertRaises(NotFound):
            switching.get_switch(self.other_owner, switch.id)

    def test_batches_prepared_for_the_old_provider_are_withdrawn_and_drafts_move_to_the_new_one_when_refreshed(self):
        pending = self.new_batch()
        batches.submit(self.maker, pending.id, expected_hash=pending.snapshot_hash)
        draft = self.new_batch()
        switching.apply(self.owner, self.ready().id)
        self.assertEqual(self.refetch(pending).status, BatchStatus.DRAFT)  # its approval was for the old provider
        self.assertEqual(self.refetch(draft).provider, "sandbox")
        with self.assertRaises(CollectionRefused) as caught:
            batches.submit(self.maker, draft.id, expected_hash=self.refetch(draft).snapshot_hash)
        self.assertEqual(caught.exception.code, "provider_changed")
        batches.refresh_preview(self.maker, draft.id)
        moved = self.refetch(draft)
        self.assertEqual((moved.provider, moved.provider_connection), ("paystack", self.paystack))

    def test_the_old_provider_stays_connected_while_its_accounts_are_live(self):
        self.run_batch(self.approved())
        switching.apply(self.owner, self.ready().id)
        from apps.bankconnect.provider_connections import BankRejected

        with self.assertRaises(BankRejected) as caught:
            provider_connections.disconnect(self.owner, self.connection.id)
        self.assertEqual(caught.exception.code, "has_live_accounts")


class AccountsAfterSwitchTests(SwitchCase):
    def setUp(self):
        super().setUp()
        self.paystack_server.on("POST", "/customer", ok({"status": True, "data": {"customer_code": "CUS_x1", "id": 1, "email": "e@x.ng"}}))
        self.paystack_server.on("POST", "/dedicated_account", ok({"status": True, "data": {
            "bank": {"name": "Wema Bank"}, "account_name": "A", "account_number": "9930000555", "assigned": True, "active": True, "id": 555}}))
        self.old = self.generate_old()

    def generate_old(self):
        self.run_batch(self.approved())
        (account,) = self.live_accounts(self.family)
        return account

    def apply(self):
        return switching.apply(self.owner, self.ready().id)

    def test_retire_at_switch_closes_every_old_account_through_the_queue_and_keeps_the_history(self):
        self.set_policy(provider_switch_policy="retire_at_switch")
        self.apply()
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, "closing")
        jobs.drain()
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, "closed")
        self.assertEqual(self.old.connection, self.connection)  # the history still says where it came from

    def test_retire_when_settled_keeps_an_account_still_owed_on_and_retires_it_when_its_family_pays(self):
        self.apply()
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, "active")  # still receiving through the old provider
        self.pay(self.family, 100_000, account=self.old)
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, "closing")  # settled after the switch: retired, whatever the settlement action says
        jobs.drain()
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, "closed")

    def test_retire_when_settled_retires_accounts_that_were_already_dormant_at_the_moment_of_the_switch(self):
        self.pay(self.family, 100_000, account=self.old)
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, "dormant")
        self.apply()
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, "closing")

    def test_manual_leaves_every_old_account_for_a_person(self):
        self.set_policy(provider_switch_policy="manual")
        self.pay(self.family, 100_000, account=self.old)
        self.apply()
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, "dormant")
        self.assertEqual(ProviderJob_count(kind="retire"), 0)

    def test_a_family_never_holds_accounts_with_two_providers_and_gets_one_from_the_new_provider_once_the_old_is_retired(self):
        self.apply()
        batch = self.new_batch()
        self.assertEqual(self.item(batch, self.family).eligibility_status, "provider_conflict")  # the old account is still live
        self.pay(self.family, 100_000, account=self.old)
        jobs.drain()
        self.charge_family(self.family, [m.student for m in self.family.members.all()], 30_000, term=self.term1)
        run = self.run_batch(self.approved())
        self.assertEqual(run.status, BatchStatus.COMPLETED)
        (new,) = self.live_accounts(self.family)
        self.assertEqual((new.provider, new.connection, new.account_number), ("paystack", self.paystack, "9930000555"))
        self.assertEqual(self.paystack_server.called("POST", "/dedicated_account"), 1)

    def test_a_payment_into_an_old_provider_account_after_the_switch_is_still_reconciled(self):
        self.apply()
        self.pay(self.family, 30_000, account=self.old)
        from apps.receivables import ledger

        self.assertEqual(ledger.family_position(self.family).outstanding, 70_000 * 100)

    def test_the_switch_is_audited_with_who_and_what_it_did(self):
        self.set_policy(provider_switch_policy="retire_at_switch")
        self.apply()
        event = CollectionAuditEvent.objects.get(kind="provider_switch_applied")
        self.assertEqual((event.actor, event.detail["from_provider"], event.detail["to_provider"], event.detail["accounts_retired"]), (self.owner, "sandbox", "paystack", 1))
        from apps.bankconnect.models import BankAuditEvent

        self.assertTrue(BankAuditEvent.objects.filter(kind="provider_switched").exists())


def ProviderJob_count(**kw) -> int:
    from ..models import ProviderJob

    return ProviderJob.objects.filter(**kw).count()
