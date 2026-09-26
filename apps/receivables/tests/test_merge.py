"""Folding one family into another: everything moves, the ledger still adds up, no account number stops working, and
only someone with billing authority may do it."""

from datetime import timedelta

from apps.academics.models import AcademicClass
from apps.bankconnect import reconciliation
from apps.bankconnect.models import BankTransaction, TransactionAllocation
from apps.students.models import GuardianLink

from .. import adjustments, allocation, collection_accounts, families, ledger, merging, schedules, statements
from ..errors import Refused
from ..models import (
    AccountStatus, FamilyCollectionAccount, FamilyCreditEntry, FamilyGuardian, FamilyStatement, FinanceAuditEvent, StudentReceivable,
)
from . import test_api
from .base import ReceivablesTestCase

N = 100


class MergeTestCase(ReceivablesTestCase):
    """The Bello household (three children, 300,000 in fees) and the Sani household (one child, 90,000)."""

    def setUp(self):
        super().setUp()
        self.bello_family()
        self.publish_bello_fees()
        self.bello = self.family
        self.primary = AcademicClass.objects.get(school=self.school, code="PRI3")
        self.yusuf = self.make_student("Yusuf", "Sani", guardian="Ada Sani", phone="08032222222")
        self.enroll(self.yusuf, self.primary, self.session)
        self.sani = families.create_family(self.school, display_name="Sani family", actor=self.owner, students=[self.yusuf])
        families.link_guardians_of(self.sani, self.yusuf)
        schedule = schedules.create_schedule(self.school, session=self.session, term=self.term, name="Sani fees", actor=self.owner)
        schedules.add_item(
            schedule, actor=self.owner, code="TUI-YUSUF", name="Tuition", category="tuition", amount_minor=90_000 * N,
            due_date=self.today + timedelta(days=20), scope="student", student=self.yusuf,
        )
        schedules.publish(schedule, actor=self.owner)
        self.sani_schedule = schedule

    def position(self, family):
        return ledger.family_position(family)

    def merge(self, source=None, into=None, reason="Same household", actor=None):
        return merging.merge(source or self.sani, into or self.bello, actor=actor or self.owner, reason=reason)

    def refresh(self, *rows):
        for row in rows:
            row.refresh_from_db()


class WhatMovesTests(MergeTestCase):
    def test_everything_moves_and_the_ledger_adds_up(self):
        allocation.allocate(self.payment(100_000 * N), self.bello)
        allocation.allocate(self.payment(30_000 * N), self.sani)
        adjustments.adjust(self.charge(self.aisha), kind="discount", amount_minor=10_000 * N, reason="Sibling", actor=self.owner)
        before = {"bello": self.position(self.bello), "sani": self.position(self.sani)}

        report = self.merge()

        merged = self.position(self.bello)
        for field in ("gross", "adjustments", "net", "paid", "outstanding", "charges"):
            self.assertEqual(getattr(merged, field), getattr(before["bello"], field) + getattr(before["sani"], field), field)
        emptied = self.position(self.sani)
        self.assertEqual((emptied.gross, emptied.paid, emptied.outstanding, emptied.charges, emptied.credit), (0, 0, 0, 0, 0))
        self.assertLedgerHolds(self.bello)
        self.assertLedgerHolds(self.sani)
        self.assertEqual(report["moved"]["charges"], 1)
        self.assertEqual(report["into"]["id"], str(self.bello.id))

    def test_every_child_is_now_in_the_one_family_and_the_old_one_is_kept_but_closed(self):
        self.merge()
        for child in (self.ahmad, self.aisha, self.maryam, self.yusuf):
            self.assertEqual(families.family_of(child), self.bello)
        self.refresh(self.sani)
        self.assertEqual((self.sani.status, self.sani.merged_into, self.sani.code[:4]), ("inactive", self.bello, "FAM-"))
        self.assertIsNotNone(self.sani.merged_at)
        self.assertEqual(families.surviving(self.sani), self.bello)
        self.assertEqual(families.surviving(self.bello), self.bello)

    def test_credit_one_family_held_pays_what_the_merged_household_owes(self):
        allocation.allocate(self.payment(150_000 * N), self.sani)  # 90,000 charge + 60,000 credit
        self.assertEqual(self.position(self.sani).credit, 60_000 * N)
        self.merge()
        merged = self.position(self.bello)
        self.assertEqual((merged.credit, merged.outstanding), (0, 300_000 * N - 60_000 * N))
        self.assertLedgerHolds(self.bello)

    def test_payments_allocations_and_statements_follow_the_charges(self):
        statement = statements.issue(self.sani, session=self.session, actor=self.owner)
        tx = self.payment(90_000 * N)
        allocation.allocate(tx, self.sani)
        self.merge()
        self.refresh(statement)
        self.assertEqual(statement.family, self.bello)
        self.assertEqual(FamilyStatement.objects.filter(family=self.sani).count(), 0)
        self.assertTrue(TransactionAllocation.objects.filter(transaction=tx).exists())
        self.assertEqual({a.family_id for a in TransactionAllocation.objects.filter(transaction=tx)}, {self.bello.id})
        built = statements.build(self.bello, session=self.session)
        self.assertEqual(len(built["students"]), 4)
        self.assertEqual({s["name"] for s in built["students"]}, {"Ahmad Bello", "Aisha Bello", "Maryam Bello", "Yusuf Sani"})

    def test_charges_are_not_charged_again_when_a_schedule_is_refreshed(self):
        before = StudentReceivable.objects.count()
        self.merge()
        self.assertEqual(schedules.refresh(self.sani_schedule, actor=self.owner).created, 0)
        self.assertEqual(StudentReceivable.objects.count(), before)
        self.assertEqual(self.charge(self.yusuf).family, self.bello)

    def test_a_charge_made_after_the_merge_goes_to_the_merged_family(self):
        self.merge()
        later = schedules.create_schedule(self.school, session=self.session, term=self.term, name="Levy", actor=self.owner)
        schedules.add_item(later, actor=self.owner, code="LEVY", name="Levy", amount_minor=5_000 * N, due_date=self.today + timedelta(days=10))
        schedules.publish(later, actor=self.owner)
        self.assertEqual({r.family_id for r in StudentReceivable.objects.filter(item_code="LEVY")}, {self.bello.id})
        self.assertLedgerHolds(self.bello)

    def test_it_is_recorded_in_the_audit_trail_with_the_reason(self):
        self.merge(reason="Registered twice under different surnames")
        event = FinanceAuditEvent.objects.get(school=self.school, kind="families_merged")
        self.assertEqual(event.object_id, str(self.bello.id))
        self.assertEqual((event.detail["reason"], event.detail["source"], event.detail["moved"]["students"]), ("Registered twice under different surnames", str(self.sani.id), 1))
        self.assertEqual(event.actor, self.owner)


class PayersTests(MergeTestCase):
    def payers(self, family):
        return list(FamilyGuardian.objects.filter(family=family, is_active=True).select_related("guardian"))

    def test_payers_move_over_and_there_is_still_exactly_one_primary(self):
        self.assertEqual(len(self.payers(self.bello)), 3)  # Musa Bello is on all three children's records
        primary_before = families.primary_payer(self.bello)
        self.merge()
        payers = self.payers(self.bello)
        self.assertIn("Ada Sani", {p.guardian.name for p in payers})
        self.assertEqual(sum(1 for p in payers if p.is_primary_payer), 1)
        self.assertEqual(families.primary_payer(self.bello), primary_before)  # the survivor's own payer stays primary
        self.assertEqual(self.payers(self.sani), [])

    def test_a_guardian_the_survivor_already_had_on_record_is_listed_once(self):
        # Yusuf was once in the Bello family: the link to his guardian exists there (stopped) as well as in the Sani family.
        families.remove_student(self.sani, self.yusuf, actor=self.owner)
        families.add_student(self.bello, self.yusuf, actor=self.owner)
        families.link_guardians_of(self.bello, self.yusuf)
        families.remove_student(self.bello, self.yusuf, actor=self.owner)
        families.add_student(self.sani, self.yusuf, actor=self.owner)
        families.link_guardians_of(self.sani, self.yusuf)
        guardian = GuardianLink.objects.get(student=self.yusuf)
        self.merge()
        self.assertEqual(FamilyGuardian.objects.filter(family=self.bello, guardian=guardian).count(), 1)
        self.assertTrue(FamilyGuardian.objects.get(family=self.bello, guardian=guardian).is_active)
        self.assertEqual(FamilyGuardian.objects.filter(family=self.sani, is_active=True).count(), 0)


class AccountsTests(MergeTestCase):
    def setUp(self):
        super().setUp()
        # A family has one live account per school, so each family has one; the Sani family also has two closed accounts on record.
        self.bello_gt = collection_accounts.register(self.bello, provider="gtbank", account_number="0000000001", actor=self.owner)
        old_uba = collection_accounts.register(self.sani, provider="uba", bank_name="UBA", account_number="0000000003", actor=self.owner)
        collection_accounts.close(old_uba, actor=self.owner, reason="Replaced")
        old_opay = collection_accounts.register(self.sani, provider="opay", account_number="0000000004", actor=self.owner)
        collection_accounts.close(old_opay, actor=self.owner, reason="Replaced")
        self.sani_gt = collection_accounts.register(self.sani, provider="gtbank", account_number="0000000002", actor=self.owner)
        self.sani_uba, self.sani_opay = old_uba, old_opay

    def reconcile(self, tx):
        reconciliation.reconcile_pending(self.school)
        return BankTransaction.objects.get(pk=tx.pk)

    def test_closed_accounts_move_over_as_history_and_the_survivors_own_account_is_the_one_shown(self):
        self.merge()
        self.refresh(self.sani_uba, self.sani_opay, self.sani_gt, self.bello_gt)
        self.assertEqual((self.sani_uba.family, self.sani_opay.family), (self.bello, self.bello))  # closed, kept as the household's history
        self.assertEqual([f["accountNumber"] for f in statements.collection_account_facts(self.bello)], ["0000000001"])

    def test_a_live_account_stays_where_it_is_when_the_survivor_already_has_one_and_still_works(self):
        self.merge()
        self.refresh(self.sani_gt)
        self.assertEqual(self.sani_gt.family, self.sani)  # one live account per family per school: it is left in place
        tx = self.reconcile(self.payment(50_000 * N, receiving_account="0000000002"))
        self.assertEqual((tx.reconciliation_status, tx.family), ("matched", self.bello))  # a number a family was given never stops working
        self.assertEqual(self.position(self.bello).paid, 50_000 * N)
        self.assertEqual(self.position(self.sani).paid, 0)
        self.assertLedgerHolds(self.bello)

    def test_the_left_account_follows_what_the_merged_family_owes(self):
        allocation.allocate(self.payment(90_000 * N), self.sani)
        self.refresh(self.sani_gt)
        self.assertEqual(self.sani_gt.status, AccountStatus.DORMANT)  # the Sani household owed nothing
        self.merge()
        self.refresh(self.sani_gt)
        self.assertEqual(self.sani_gt.status, AccountStatus.ACTIVE)  # the merged household owes again

    def test_a_live_account_moves_when_the_survivor_has_none(self):
        self.bello_gt = FamilyCollectionAccount.objects.get(family=self.bello, status="active")
        collection_accounts.close(self.bello_gt, actor=self.owner, reason="Retired")
        self.merge()
        self.refresh(self.sani_gt)
        self.assertEqual(self.sani_gt.family, self.bello)

    def test_the_preview_says_which_accounts_move_and_which_stay(self):
        preview = merging.preview(self.sani, self.bello, actor=self.members["accountant"])
        self.assertEqual(preview["accountsMoved"], [])
        self.assertEqual({a["provider"] for a in preview["accountsKeptAsIs"]}, {"gtbank"})


class ChainTests(MergeTestCase):
    def test_merges_stay_one_level_deep_and_an_old_number_still_lands_on_the_survivor(self):
        collection_accounts.register(self.bello, provider="gtbank", account_number="0000000001", actor=self.owner)
        collection_accounts.register(self.sani, provider="gtbank", account_number="0000000002", actor=self.owner)
        carter_student = self.make_student("Bola", "Carter", guardian="Tunde Carter", phone="08033333333")
        self.enroll(carter_student, self.primary, self.session)
        carter = families.create_family(self.school, display_name="Carter family", actor=self.owner, students=[carter_student])
        self.merge()  # Sani -> Bello
        self.merge(source=self.bello, into=carter)  # Bello -> Carter
        self.refresh(self.sani, self.bello)
        self.assertEqual((self.sani.merged_into, self.bello.merged_into), (carter, carter))
        self.assertEqual(families.surviving(self.sani), carter)
        reconciliation.reconcile_pending(self.school)
        tx = self.payment(20_000 * N, receiving_account="0000000002")
        reconciliation.reconcile_pending(self.school)
        self.assertEqual(BankTransaction.objects.get(pk=tx.pk).family, carter)
        self.assertEqual(len(families.active_students(carter)), 5)
        self.assertLedgerHolds(carter)


class RefusalTests(MergeTestCase):
    def test_a_reason_is_required(self):
        for reason in ("", "  ", None):
            with self.assertRaises(Refused) as caught:
                self.merge(reason=reason)
            self.assertEqual(caught.exception.code, "reason_required")
        with self.assertRaises(Refused) as caught:
            self.merge(reason="x" * 301)
        self.assertEqual(caught.exception.code, "reason_too_long")

    def test_a_family_cannot_be_merged_into_itself_or_twice(self):
        with self.assertRaises(Refused) as caught:
            self.merge(source=self.bello, into=self.bello)
        self.assertEqual(caught.exception.code, "same_family")
        self.merge()
        with self.assertRaises(Refused) as caught:
            self.merge()
        self.assertEqual(caught.exception.code, "already_merged")
        third = families.create_family(self.school, display_name="Another", actor=self.owner)
        with self.assertRaises(Refused) as caught:
            self.merge(source=third, into=self.sani)
        self.assertEqual(caught.exception.code, "target_merged")

    def test_a_closed_family_on_either_side_is_refused(self):
        families.set_status(self.sani, "inactive", actor=self.owner)
        with self.assertRaises(Refused) as caught:
            self.merge()
        self.assertEqual(caught.exception.code, "family_closed")
        families.set_status(self.sani, "active", actor=self.owner)
        families.set_status(self.bello, "inactive", actor=self.owner)
        with self.assertRaises(Refused) as caught:
            self.merge()
        self.assertEqual(caught.exception.code, "target_closed")

    def test_families_of_two_schools_are_never_merged(self):
        _, _, other_primary, _ = self.make_year(self.other_school)
        stranger = self.make_student("Zed", "Other", school=self.other_school)
        outsider = families.create_family(self.other_school, display_name="Elsewhere", actor=self.other_owner, students=[stranger])
        with self.assertRaises(Refused) as caught:
            self.merge(into=outsider)
        self.assertEqual(caught.exception.code, "family_not_found")
        self.assertEqual(families.family_of(stranger), outsider)

    def test_only_billing_authority_may_merge_not_even_the_finance_office(self):
        for who in (self.members["accountant"], self.members["principal"], self.members["teacher"], self.members["parent"]):
            with self.assertRaises(Refused, msg=who.role) as caught:
                self.merge(actor=who)
            self.assertEqual(caught.exception.code, "not_billing_authority")
        self.give_duty(self.members["principal"])
        self.merge(actor=self.members["principal"])  # a delegate the owner named can
        self.assertEqual(families.family_of(self.yusuf), self.bello)

    def test_a_refused_merge_changes_nothing(self):
        before = (StudentReceivable.objects.filter(family=self.sani).count(), FamilyCreditEntry.objects.count())
        with self.assertRaises(Refused):
            self.merge(reason="")
        self.assertEqual((StudentReceivable.objects.filter(family=self.sani).count(), FamilyCreditEntry.objects.count()), before)
        self.assertEqual(families.family_of(self.yusuf), self.sani)

    def test_the_preview_lists_what_would_refuse_it_and_changes_nothing(self):
        preview = merging.preview(self.bello, self.bello, actor=self.owner)
        self.assertEqual([p["code"] for p in preview["problems"]], ["same_family"])
        good = merging.preview(self.sani, self.bello, actor=self.members["accountant"])
        self.assertEqual(good["problems"], [])
        self.assertEqual((good["moves"]["students"], good["moves"]["charges"]), (1, 1))
        self.assertTrue(good["irreversible"])
        self.assertEqual(families.family_of(self.yusuf), self.sani)
        with self.assertRaises(Refused):
            merging.preview(self.sani, self.bello, actor=self.members["teacher"])


class MergeApiTests(test_api.ApiTestCase):
    def setUp(self):
        super().setUp()
        self.with_fees()
        self.bello = self.family
        self.yusuf = self.make_student("Yusuf", "Sani", guardian="Ada Sani", phone="08032222222")
        primary = AcademicClass.objects.get(school=self.school, code="PRI3")
        self.enroll(self.yusuf, primary, self.session)
        self.sani = families.create_family(self.school, display_name="Sani family", actor=self.owner, students=[self.yusuf])
        families.link_guardians_of(self.sani, self.yusuf)

    def test_preview_and_merge_over_the_api(self):
        preview = self.get(f"families/{self.sani.id}/merge-preview/?into={self.bello.id}", who=self.finance).json()["preview"]
        self.assertEqual((preview["problems"], preview["moves"]["students"]), ([], 1))
        self.assertEqual(self.post(f"families/{self.sani.id}/merge/", {"intoFamilyId": str(self.bello.id), "reason": "Same household"}, who=self.finance).status_code, 403)
        done = self.post(f"families/{self.sani.id}/merge/", {"intoFamilyId": str(self.bello.id), "reason": "Same household"})
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.json()["merge"]["source"]["mergedInto"], str(self.bello.id))
        listed = {f["id"]: f for f in self.get("families/").json()["families"]}
        self.assertEqual((listed[str(self.sani.id)]["status"], listed[str(self.sani.id)]["mergedInto"]), ("inactive", str(self.bello.id)))
        again = self.post(f"families/{self.sani.id}/merge/", {"intoFamilyId": str(self.bello.id), "reason": "Again"})
        self.assertEqual((again.status_code, again.json()["code"]), (400, "already_merged"))

    def test_a_reason_is_required_and_another_schools_family_is_not_found(self):
        self.assertEqual(self.post(f"families/{self.sani.id}/merge/", {"intoFamilyId": str(self.bello.id)}).json()["code"], "reason_required")
        theirs = families.create_family(self.other_school, display_name="Elsewhere", actor=self.other_owner)
        self.assertEqual(self.post(f"families/{self.sani.id}/merge/", {"intoFamilyId": str(theirs.id), "reason": "x"}).status_code, 404)
        self.assertEqual(self.get(f"families/{self.sani.id}/merge-preview/?into={theirs.id}").status_code, 404)
        self.assertEqual(self.post(f"families/{self.sani.id}/merge/", {"reason": "x"}).status_code, 400)

    def test_a_parent_of_a_merged_in_child_now_sees_the_one_family_with_all_the_children(self):
        parent = self.members["parent"]
        GuardianLink.objects.filter(student=self.yusuf).update(account_user=parent.user)
        GuardianLink.objects.filter(student__in=[self.ahmad, self.aisha, self.maryam]).update(account_user=parent.user)
        self.assertEqual(len(self.get("me/families/", who=parent).json()["families"]), 2)  # two households before
        self.post(f"families/{self.sani.id}/merge/", {"intoFamilyId": str(self.bello.id), "reason": "Same household"})
        mine = self.get("me/families/", who=parent).json()["families"]
        self.assertEqual([f["id"] for f in mine], [str(self.bello.id)])
        statement = self.get(f"me/families/{self.bello.id}/statement/", who=parent).json()["statement"]
        self.assertEqual(len(statement["students"]), 3)  # a statement lists children who have charges: Yusuf has none yet
        self.assertEqual(len(families.active_students(self.bello)), 4)
        self.assertEqual(self.get(f"me/families/{self.sani.id}/statement/", who=parent).status_code, 404)
