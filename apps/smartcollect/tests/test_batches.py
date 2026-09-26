"""Preparing a collection batch: who is in the preview, what each family owes, whether it is eligible and why, what its account would be asked to
collect, and how a maker selects, overrides and chooses balances. Nothing here reaches a provider or changes the ledger."""

from apps.receivables import ledger
from apps.receivables.models import FamilyCollectionAccount

from .. import batches, policy
from ..constants import Eligibility, GenerationStatus
from ..errors import CollectionRefused
from ..models import CollectionAuditEvent
from .base import N, POLICY, CollectTestCase


class PreviewTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.alpha = self.make_family("Alpha", owes=100_000)
        self.bravo = self.make_family("Bravo", owes=50_000, students=2)

    def test_a_draft_is_made_for_the_active_provider_and_lists_every_active_family(self):
        batch = self.new_batch(title="Term 1 accounts")
        self.assertEqual((batch.status, batch.version, batch.provider, batch.environment, batch.provider_connection), ("draft", 1, "sandbox", "test", self.connection))
        self.assertEqual((batch.family_count, batch.selected_count), (2, 2))
        self.assertEqual(len(batch.snapshot_hash), 64)
        self.assertEqual(batch.prepared_by, self.maker)

    def test_each_family_shows_what_it_owes_and_what_its_account_would_collect(self):
        batch = self.new_batch()
        alpha, bravo = self.item(batch, self.alpha), self.item(batch, self.bravo)
        self.assertEqual((alpha.previous_arrears_minor, alpha.current_due_minor, alpha.proposed_collection_minor), (0, 100_000 * N, 100_000 * N))
        self.assertEqual((bravo.current_due_minor, bravo.proposed_collection_minor), (100_000 * N, 100_000 * N))  # two children, 50,000 each
        self.assertEqual((batch.total_current_due_minor, batch.total_collection_minor), (200_000 * N, 200_000 * N))
        self.assertTrue(alpha.selected and alpha.eligibility_status == Eligibility.ELIGIBLE and alpha.generation_status == GenerationStatus.PENDING)

    def test_no_provider_is_called_and_the_ledger_is_untouched_by_a_preview(self):
        before = ledger.family_position(self.alpha)
        self.new_batch()
        self.assertEqual(ledger.family_position(self.alpha), before)
        self.assertEqual(self.sandbox_accounts().count(), 0)
        self.assertEqual(FamilyCollectionAccount.objects.count(), 0)

    def test_the_batch_is_for_one_session_and_term_of_this_school_and_needs_an_active_provider(self):
        with self.assertRaises(CollectionRefused) as caught:
            batches.create_batch(self.maker, session=None)
        self.assertEqual(caught.exception.code, "session_required")
        with self.assertRaises(CollectionRefused) as caught:
            batches.create_batch(self.maker, session=self.session, term=self.other_school_term())
        self.assertEqual(caught.exception.code, "term_not_in_session")

    def other_school_term(self):
        from apps.academics.models import AcademicSession, AcademicTerm
        from datetime import date

        session = AcademicSession.objects.create(school=self.other_school, code="S", name="Other", starts_on=date(2026, 9, 1), ends_on=date(2027, 7, 1))
        return AcademicTerm.objects.create(session=session, code="T", name="T", sequence=1, starts_on=date(2026, 9, 1), ends_on=date(2026, 12, 1))

    def test_without_an_active_provider_nothing_can_be_prepared(self):
        self.connection.is_active_provider = False
        self.connection.save()
        with self.assertRaises(CollectionRefused) as caught:
            self.new_batch()
        self.assertEqual(caught.exception.code, "no_active_provider")

    def test_a_provider_that_is_not_working_cannot_be_prepared_for(self):
        self.connection.status = "needs_reauth"
        self.connection.save()
        with self.assertRaises(CollectionRefused) as caught:
            self.new_batch()
        self.assertEqual(caught.exception.code, "provider_not_connected")

    def test_only_someone_who_prepares_can_start_a_batch(self):
        for who in (self.checker, self.members["teacher"], self.members["parent"]):
            with self.assertRaises(CollectionRefused) as caught:
                self.new_batch(who=who)
            self.assertEqual(caught.exception.code, "not_preparer")
        self.new_batch(who=self.owner)

    def test_inactive_or_merged_families_and_families_without_students_are_left_out(self):
        from apps.receivables import families

        empty = families.create_family(self.school, display_name="Empty family", actor=self.owner)
        gone = self.make_family("Gone")
        families.set_status(gone, "inactive", actor=self.owner)
        batch = self.new_batch()
        ids = set(self.items(batch))
        self.assertEqual(ids, {self.alpha.id, self.bravo.id})
        self.assertNotIn(empty.id, ids)

    def test_refreshing_picks_up_a_new_family_and_keeps_what_the_maker_chose(self):
        batch = self.new_batch()
        batches.set_selection(self.maker, batch.id, deselect=[str(self.item(batch, self.alpha).id)])
        newcomer = self.make_family("Newcomer", owes=10_000)
        batches.refresh_preview(self.maker, batch.id)
        after = self.items(batch)
        self.assertFalse(after[self.alpha.id].selected)  # the maker's choice is kept
        self.assertTrue(after[newcomer.id].selected)  # the new family is offered

    def test_version_rises_only_when_what_would_be_generated_changes(self):
        batch = self.new_batch()
        again = batches.refresh_preview(self.maker, batch.id)
        self.assertEqual((again.version, again.snapshot_hash), (batch.version, batch.snapshot_hash))
        batches.set_selection(self.maker, batch.id, deselect=[str(self.item(batch, self.alpha).id)])
        self.assertEqual(self.refetch(batch).version, batch.version + 1)

    def test_the_preview_is_deterministic(self):
        first = self.new_batch()
        second = self.new_batch()
        # different batches (the id is part of the fingerprint) but each is stable when refreshed
        self.assertNotEqual(first.snapshot_hash, second.snapshot_hash)
        self.assertEqual(batches.live_hash(first), first.snapshot_hash)
        self.assertEqual(batches.live_hash(second), second.snapshot_hash)


class EligibilityTests(CollectTestCase):
    """A family that still owes for an earlier term is not automatically given an account for the new one."""

    def setUp(self):
        super().setUp()
        self.owing = self.make_family("Owing", owes=100_000, term=self.term1)  # unpaid in term 1
        self.paid = self.make_family("Paid", owes=100_000, term=self.term1)
        self.into_term_two()
        # second-term fees for both
        for family in (self.owing, self.paid):
            kids = [m.student for m in family.members.all()]
            self.charge_family(family, kids, 60_000, term=self.term2)

    def batch(self, **kw):
        return self.new_batch(term=self.term2, **kw)

    def test_the_default_needs_an_override_for_a_family_that_owes_for_an_earlier_term(self):
        batch = self.batch()
        item = self.item(batch, self.owing)
        self.assertEqual(item.eligibility_status, Eligibility.NEEDS_OVERRIDE)
        self.assertEqual((item.previous_arrears_minor, item.current_due_minor), (100_000 * N, 60_000 * N))
        self.assertFalse(item.selected)

    def test_a_family_with_no_earlier_balance_is_eligible(self):
        self.pay_all(self.paid)
        batch = self.batch()
        self.assertEqual(self.item(batch, self.paid).eligibility_status, Eligibility.ELIGIBLE)

    def pay_all(self, family):
        """The family pays what it owed for the first term, into the account the provider made for it."""
        from apps.receivables import collection_accounts

        account = self.pay(family, 100_000, account=self.provider_account(family))
        collection_accounts.mark_closed(account, actor=self.owner, reason="Not needed for this test")

    def provider_account(self, family):
        from apps.receivables import collection_accounts
        from apps.bankconnect.providers.base import ProvisionedAccount

        number = f"9{family.code[-8:].replace('-', '0')}"[:10]
        return collection_accounts.create_from_provider(
            family, self.connection, ProvisionedAccount(account_number=number, provider_account_ref=f"REF-{family.code}", lookup_ref=number),
            actor=self.owner, idempotency_key=f"seed-{family.id}",
        )

    def test_the_policy_can_exclude_include_or_ask_for_each_manually(self):
        expected = {
            "exclude": Eligibility.EXCLUDED, "needs_override": Eligibility.NEEDS_OVERRIDE, "include": Eligibility.ELIGIBLE,
            "manual_approval": Eligibility.MANUAL_APPROVAL,
        }
        for rule, status in expected.items():
            self.set_policy(eligibility_policy=rule)
            batch = self.batch()
            self.assertEqual(self.item(batch, self.owing).eligibility_status, status, rule)

    def test_a_family_override_changes_that_one_family_only_and_is_audited(self):
        policy.set_override(self.owner, scope="family", family=self.owing, values={"eligibility_policy": "include"}, reason="Agreed payment plan")
        batch = self.batch()
        self.assertEqual(self.item(batch, self.owing).eligibility_status, Eligibility.ELIGIBLE)
        self.assertEqual(self.item(batch, self.owing).policy_snapshot["sources"]["eligibility_policy"]["scope"], "family")
        self.assertEqual(self.item(batch, self.paid).policy_snapshot["sources"]["eligibility_policy"]["scope"], "school")

    def test_overriding_a_family_includes_it_with_a_reason_and_leaves_the_arrears_owed(self):
        batch = self.batch()
        item = self.item(batch, self.owing)
        before = ledger.family_position(self.owing)
        batches.set_eligibility_override(self.maker, batch.id, item.id, reason="Head teacher agreed")
        item.refresh_from_db()
        self.assertEqual((item.eligibility_override, item.override_reason, item.override_by, item.selected), (True, "Head teacher agreed", self.maker, True))
        self.assertEqual(item.eligibility_status, Eligibility.NEEDS_OVERRIDE)  # still says why it needed one
        self.assertEqual(ledger.family_position(self.owing), before)  # nothing about what it owes changed
        self.assertTrue(CollectionAuditEvent.objects.filter(kind="eligibility_overridden", detail__family=str(self.owing.id)).exists())

    def test_an_override_needs_a_reason_and_only_a_family_that_needs_one(self):
        batch = self.batch()
        with self.assertRaises(CollectionRefused) as caught:
            batches.set_eligibility_override(self.maker, batch.id, self.item(batch, self.owing).id, reason="")
        self.assertEqual(caught.exception.code, "reason_required")
        self.pay_all(self.paid)
        batches.refresh_preview(self.maker, batch.id)
        with self.assertRaises(CollectionRefused) as caught:
            batches.set_eligibility_override(self.maker, batch.id, self.item(batch, self.paid).id, reason="x")
        self.assertEqual(caught.exception.code, "no_override_needed")

    def test_a_family_the_policy_excludes_can_only_be_overridden_by_someone_who_manages_the_policy(self):
        self.set_policy(eligibility_policy="exclude")
        batch = self.batch()
        item = self.item(batch, self.owing)
        with self.assertRaises(CollectionRefused) as caught:
            batches.set_eligibility_override(self.maker, batch.id, item.id, reason="Just this once")
        self.assertEqual(caught.exception.code, "not_policy_manager")
        self.give_duties(self.maker, "finance.collection_prepare", POLICY)
        batches.set_eligibility_override(self.maker, batch.id, item.id, reason="Just this once")
        self.assertTrue(self.item(batch, self.owing).selected)

    def test_a_family_that_needs_an_override_cannot_be_selected_without_one(self):
        batch = self.batch()
        item = self.item(batch, self.owing)
        with self.assertRaises(CollectionRefused) as caught:
            batches.set_selection(self.maker, batch.id, select=[str(item.id)])
        self.assertEqual(caught.exception.code, "cannot_select")

    def test_clearing_an_override_takes_the_family_out_again(self):
        batch = self.batch()
        item = self.item(batch, self.owing)
        batches.set_eligibility_override(self.maker, batch.id, item.id, reason="Agreed")
        batches.clear_eligibility_override(self.maker, batch.id, item.id)
        item.refresh_from_db()
        self.assertEqual((item.eligibility_override, item.selected), (False, False))

    def test_manual_approval_families_can_be_selected_but_are_approved_one_by_one_by_the_checker(self):
        self.set_policy(eligibility_policy="manual_approval")
        batch = self.batch()
        item = self.item(batch, self.owing)
        batches.set_selection(self.maker, batch.id, select=[str(item.id)])
        batch = self.refetch(batch)
        batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        with self.assertRaises(CollectionRefused) as caught:
            batches.approve(self.checker, batch.id, expected_hash=self.refetch(batch).snapshot_hash)
        self.assertEqual(caught.exception.code, "manual_approval_needed")
        self.assertEqual(caught.exception.extra["itemIds"], [str(item.id)])
        batches.approve(self.checker, batch.id, expected_hash=self.refetch(batch).snapshot_hash, manual_item_ids=[str(item.id)])
        item.refresh_from_db()
        self.assertEqual(item.manual_approved_by, self.checker)


class ArrearsTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.family = self.make_family("Owing", owes=100_000, term=self.term1)
        self.into_term_two()
        self.charge_family(self.family, [m.student for m in self.family.members.all()], 60_000, term=self.term2)
        self.set_policy(eligibility_policy="include")

    def owing_item(self):
        batch = self.new_batch(term=self.term2)
        return batch, CollectionGenerationItemFor(batch, self.family)

    def test_carry_forward_puts_the_previous_balance_into_the_target(self):
        _, item = self.owing_item()
        self.assertEqual((item.previous_arrears_minor, item.current_due_minor, item.proposed_collection_minor), (100_000 * N, 60_000 * N, 160_000 * N))

    def test_current_term_only_leaves_it_out_and_the_ledger_still_shows_it_owed(self):
        self.set_policy(arrears_policy="current_term_only")
        _, item = self.owing_item()
        self.assertEqual((item.previous_arrears_minor, item.proposed_collection_minor), (100_000 * N, 60_000 * N))
        self.assertEqual(ledger.family_position(self.family).arrears, 100_000 * N)

    def test_custom_selection_carries_only_the_balances_a_person_chose(self):
        self.set_policy(arrears_policy="custom_selection")
        batch, item = self.owing_item()
        self.assertEqual(item.proposed_collection_minor, 60_000 * N)  # nothing chosen yet
        breakdown = item.arrears_breakdown
        self.assertEqual(len(breakdown), 1)
        batches.choose_arrears(self.maker, batch.id, item.id, receivable_ids=[breakdown[0]["receivableId"]])
        item.refresh_from_db()
        self.assertEqual(item.proposed_collection_minor, 160_000 * N)

    def test_custom_selection_refuses_a_balance_the_family_does_not_owe(self):
        self.set_policy(arrears_policy="custom_selection")
        batch, item = self.owing_item()
        with self.assertRaises(CollectionRefused) as caught:
            batches.choose_arrears(self.maker, batch.id, item.id, receivable_ids=["00000000-0000-0000-0000-000000000000"])
        self.assertEqual(caught.exception.code, "invalid_arrears")

    def test_choosing_is_only_offered_where_the_policy_lets_a_person_choose(self):
        batch, item = self.owing_item()
        with self.assertRaises(CollectionRefused) as caught:
            batches.choose_arrears(self.maker, batch.id, item.id, receivable_ids=[])
        self.assertEqual(caught.exception.code, "not_custom_arrears")

    def test_the_arrears_treatment_can_differ_by_family(self):
        other = self.make_family("Other", owes=50_000, term=self.term1)
        self.charge_family(other, [m.student for m in other.members.all()], 20_000, term=self.term2)
        policy.set_override(self.owner, scope="family", family=other, values={"arrears_policy": "current_term_only"}, reason="Old debt handled separately")
        batch = self.new_batch(term=self.term2)
        self.assertEqual(self.item(batch, self.family).proposed_collection_minor, 160_000 * N)
        self.assertEqual(self.item(batch, other).proposed_collection_minor, 20_000 * N)

    def test_arrears_are_never_written_into_the_ledger_by_any_treatment(self):
        before = ledger.family_position(self.family)
        for treatment in ("carry_forward", "current_term_only", "custom_selection"):
            self.set_policy(arrears_policy=treatment)
            self.new_batch(term=self.term2)
        self.assertEqual(ledger.family_position(self.family), before)
        self.assertEqual(ledger.verify_family(self.family), [])


def CollectionGenerationItemFor(batch, family):
    from ..models import CollectionGenerationBatchItem

    return CollectionGenerationBatchItem.objects.get(batch=batch, family=family)


class ExistingAccountTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.family = self.make_family("Alpha", owes=100_000)

    def account(self, connection=None, **over):
        from apps.bankconnect.providers.base import ProvisionedAccount
        from apps.receivables import collection_accounts

        return collection_accounts.create_from_provider(
            self.family, connection or self.connection, ProvisionedAccount(account_number="9000000001", provider_account_ref="REF-1", lookup_ref="9000000001"),
            actor=self.owner, idempotency_key="seed-1", **over,
        )

    def status_of(self):
        return self.item(self.new_batch(), self.family)

    def test_a_static_account_that_still_covers_the_period_is_kept_and_nothing_is_made(self):
        self.account(reuse_scope="until_replaced")
        item = self.status_of()
        self.assertEqual((item.eligibility_status, item.selected), (Eligibility.HAS_ACCOUNT, False))

    def test_a_static_account_promised_for_one_term_does_not_cover_the_next_and_is_replaced(self):
        self.account(scope_session=self.session, scope_term=self.term1, reuse_scope="one_term")
        self.into_term_two()
        self.set_policy(eligibility_policy="include")  # the first term's fees are still owed
        batch = self.new_batch(term=self.term2)
        item = self.item(batch, self.family)
        self.assertEqual((item.eligibility_status, item.policy_snapshot["existingAction"]), (Eligibility.ELIGIBLE, "replace"))

    def test_the_same_account_still_covers_a_second_term_when_promised_for_two(self):
        self.account(scope_session=self.session, scope_term=self.term1, reuse_scope="selected_terms", reuse_count=2)
        self.into_term_two()
        self.assertEqual(self.item(self.new_batch(term=self.term2), self.family).eligibility_status, Eligibility.HAS_ACCOUNT)

    def test_a_dynamic_account_covers_only_its_own_term(self):
        self.account(mode="dynamic", scope_session=self.session, scope_term=self.term1)
        self.assertEqual(self.item(self.new_batch(term=self.term1), self.family).eligibility_status, Eligibility.HAS_ACCOUNT)
        self.into_term_two()
        self.set_policy(eligibility_policy="include")
        self.assertEqual(self.item(self.new_batch(term=self.term2), self.family).policy_snapshot["existingAction"], "replace")

    def test_an_account_with_another_provider_or_recorded_by_hand_must_be_retired_first(self):
        other = CollectionProviderConnectionFor(self)
        FamilyCollectionAccount.objects.filter(pk=self.account().pk).update(connection=other, provider="paystack")
        item = self.status_of()
        self.assertEqual((item.eligibility_status, item.selected), (Eligibility.PROVIDER_CONFLICT, False))
        with self.assertRaises(CollectionRefused):
            batches.set_selection(self.maker, item.batch_id, select=[str(item.id)])

    def test_a_suspended_or_closing_account_is_not_replaced_behind_anyones_back(self):
        account = self.account()
        for state in ("suspended", "closing"):
            FamilyCollectionAccount.objects.filter(pk=account.pk).update(status=state)
            self.assertEqual(self.status_of().eligibility_status, Eligibility.PROVIDER_CONFLICT, state)


def CollectionProviderConnectionFor(case):
    from apps.bankconnect.models import CollectionProviderConnection

    return CollectionProviderConnection.objects.create(school=case.school, provider="paystack", environment="test", status="connected", is_sandbox=False)


class DetailsAndCapabilityTests(CollectTestCase):
    def test_a_family_without_the_payer_details_the_provider_needs_is_not_selectable_and_says_what_is_missing(self):
        family = self.make_family("Nomail", email=None)
        batch = self.new_batch()
        item = self.item(batch, family)
        self.assertEqual((item.eligibility_status, item.selected), (Eligibility.MISSING_DETAILS, False))
        self.assertIn("email", item.eligibility_note)
        with self.assertRaises(CollectionRefused):
            batches.set_selection(self.maker, batch.id, select=[str(item.id)])

    def test_adding_the_details_and_refreshing_makes_the_family_eligible(self):
        from apps.students.models import GuardianLink

        family = self.make_family("Nomail", email=None)
        batch = self.new_batch()
        GuardianLink.objects.filter(student__family_memberships__family=family).update(email="a@b.ng")
        batches.refresh_preview(self.maker, batch.id)
        self.assertEqual(self.item(batch, family).eligibility_status, Eligibility.ELIGIBLE)

    def test_a_static_account_type_is_not_offered_for_a_provider_that_only_makes_dynamic_ones(self):
        from apps.bankconnect.providers import registry
        from unittest import mock
        from dataclasses import replace

        family = self.make_family("Alpha")
        connector = registry.get_connector("sandbox")
        caps = replace(connector.info.capabilities, supports_static_accounts=False)
        with mock.patch.object(type(connector), "info", replace(connector.info, capabilities=caps)):
            item = self.item(self.new_batch(), family)
            self.assertEqual(item.eligibility_status, Eligibility.UNSUPPORTED_MODE)
            self.set_policy(account_mode="dynamic")
            batch = self.new_batch()
            self.assertEqual(self.item(batch, family).eligibility_status, Eligibility.ELIGIBLE)

    def test_a_provider_that_makes_an_account_for_an_amount_has_nothing_to_generate_for_a_family_owing_nothing(self):
        from apps.bankconnect.providers import registry
        from unittest import mock
        from dataclasses import replace

        family = self.make_family("Free", owes=0)
        connector = registry.get_connector("sandbox")
        with mock.patch.object(type(connector), "info", replace(connector.info, requires_amount=True)):
            batch = self.new_batch()
            item = self.item(batch, family)
            self.assertEqual(item.eligibility_status, Eligibility.NOTHING_DUE)
            with self.assertRaises(CollectionRefused):
                batches.set_selection(self.maker, batch.id, select=[str(item.id)])

    def test_a_family_owing_nothing_can_still_be_given_a_reusable_account_if_the_school_wants_one(self):
        family = self.make_family("Free", owes=0)
        batch = self.new_batch()
        item = self.item(batch, family)
        self.assertEqual((item.eligibility_status, item.selected), (Eligibility.NOTHING_DUE, False))
        batches.set_selection(self.maker, batch.id, select=[str(item.id)])
        self.assertTrue(self.item(batch, family).selected)


class SelectionTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.a, self.b, self.c = self.make_family("Alpha"), self.make_family("Bravo"), self.make_family("Charlie")
        self.batch = self.new_batch()

    def ids(self, *families):
        return [str(self.item(self.batch, f).id) for f in families]

    def test_families_are_selected_and_deselected_and_the_totals_follow(self):
        batches.set_selection(self.maker, self.batch.id, deselect=self.ids(self.a, self.b))
        batch = self.refetch(self.batch)
        self.assertEqual((batch.selected_count, batch.total_collection_minor), (1, 100_000 * N))
        batches.set_selection(self.maker, self.batch.id, select=self.ids(self.a))
        self.assertEqual(self.refetch(self.batch).selected_count, 2)

    def test_deselect_all_and_select_all_eligible(self):
        batches.set_selection(self.maker, self.batch.id, deselect_all=True)
        self.assertEqual(self.refetch(self.batch).selected_count, 0)
        batches.set_selection(self.maker, self.batch.id, select_all_eligible=True)
        self.assertEqual(self.refetch(self.batch).selected_count, 3)

    def test_a_deselected_family_is_skipped_and_never_generated(self):
        batches.set_selection(self.maker, self.batch.id, deselect=self.ids(self.a))
        self.assertEqual(self.item(self.batch, self.a).generation_status, GenerationStatus.SKIPPED)

    def test_a_screen_that_is_out_of_date_is_refused_and_told_the_current_version(self):
        batches.set_selection(self.maker, self.batch.id, deselect=self.ids(self.a))
        with self.assertRaises(CollectionRefused) as caught:
            batches.set_selection(self.maker, self.batch.id, deselect=self.ids(self.b), expected_version=self.batch.version)
        self.assertEqual(caught.exception.code, "stale_preview")
        self.assertEqual(caught.exception.extra["version"], self.refetch(self.batch).version)

    def test_a_family_from_another_batch_or_school_is_not_found(self):
        with self.assertRaises(CollectionRefused) as caught:
            batches.set_selection(self.maker, self.batch.id, select=["00000000-0000-0000-0000-000000000000"])
        self.assertEqual(caught.exception.code, "item_not_found")

    def test_another_schools_batch_is_a_404(self):
        from rest_framework.exceptions import NotFound

        with self.assertRaises(NotFound):
            batches.get_batch(self.other_owner, self.batch.id)

    def test_only_a_draft_or_rejected_batch_can_be_changed(self):
        batches.submit(self.maker, self.batch.id, expected_hash=self.batch.snapshot_hash)
        with self.assertRaises(CollectionRefused) as caught:
            batches.set_selection(self.maker, self.batch.id, deselect=self.ids(self.a))
        self.assertEqual(caught.exception.code, "not_editable")

    def test_the_batch_can_be_cancelled_before_it_runs_and_then_nothing_is_selected(self):
        batches.cancel(self.maker, self.batch.id, reason="Wrong term")
        batch = self.refetch(self.batch)
        self.assertEqual(batch.status, "cancelled")
        self.assertFalse(any(i.selected for i in self.items(batch).values()))
        with self.assertRaises(CollectionRefused):
            batches.set_selection(self.maker, self.batch.id, select=self.ids(self.a))
