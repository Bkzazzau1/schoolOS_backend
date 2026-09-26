"""A family's collection account: one per family per school (never one per child), what a parent is shown, how a provider names its
number, and voiding statements.

School fees are the school's own money: every account here belongs to the school's own provider connection. Nothing in this file (or
the code it tests) involves SchoolOS's own income, which is `apps.billing`. How a family's account is MADE (the active provider, the
approved batch) is covered by `apps.smartcollect`; this file is about what an account is once it exists.
"""

import json

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from apps.students.models import GuardianLink

from .. import account_shapes, allocation, collection_accounts, families, ledger, statements
from ..account_shapes import AccountShape, clean_details
from ..errors import Refused
from ..models import AccountStatus, FamilyCollectionAccount, FamilyStatement, FinanceAuditEvent
from . import test_api
from .base import ReceivablesTestCase

N = 100
User = get_user_model()


class ShapeTests(ReceivablesTestCase):
    def test_the_default_shape_does_not_assume_ten_digits(self):
        shape = account_shapes.GENERIC
        for number in ("0123456789", "8012345678", "WEMA-4471-2209", "FAM/K7Q2/01", "PAY4471", "140008260136"):
            self.assertEqual(shape.clean_number(number), number)

    def test_spaces_a_person_typed_are_removed_because_a_payer_types_it_without_them(self):
        self.assertEqual(account_shapes.GENERIC.clean_number("0123 456 789 "), "0123456789")

    def test_nonsense_is_refused_with_the_providers_own_word_for_the_number(self):
        shape = AccountShape(number_label="Payment code", number_example="PAY-1234")
        for bad in ("12", "a b#c!", "x" * 60, "-abcd"):
            with self.assertRaises(Refused) as caught:
                shape.clean_number(bad)
            self.assertEqual(caught.exception.code, "invalid_account_number")
            self.assertIn("payment code", caught.exception.message)
        self.assertEqual(shape.clean_number(""), "")  # no number is allowed where the provider gives a reference instead

    def test_a_provider_that_documents_a_format_can_narrow_it(self):
        shape = AccountShape(number_pattern=r"\d{10}", number_example="0123456789")
        self.assertEqual(shape.clean_number("0123456789"), "0123456789")
        with self.assertRaises(Refused):
            shape.clean_number("WEMA-1234")

    def test_each_supported_provider_names_the_number_a_payer_uses(self):
        self.assertEqual(account_shapes.label_for("paystack"), "Account number")
        self.assertEqual(account_shapes.label_for("monnify"), "Account number")
        self.assertEqual(account_shapes.note_for("remita"), "")  # Remita is not a Smart Money Collection provider: it gets the plain word
        self.assertEqual(account_shapes.label_for("remita"), "Account number")
        self.assertEqual(account_shapes.label_for("some-old-bank"), "Account number")  # a provider SchoolOS no longer knows: the plain word

    def test_extra_facts_for_the_payer_are_kept_tidy_and_limited(self):
        self.assertEqual(clean_details([{"label": " Payment  reference ", "value": "BG 0042"}]), [{"label": "Payment reference", "value": "BG 0042"}])
        self.assertEqual(clean_details(None), [])
        for bad in ([{"label": "", "value": "x"}], [{"label": "x"}], ["text"], [{"label": "A", "value": "1"}, {"label": "a", "value": "2"}],
                    [{"label": "x" * 41, "value": "1"}], [{"label": "a", "value": "y" * 121}],
                    [{"label": str(i), "value": "v"} for i in range(account_shapes.MAX_DETAILS + 1)], "not a list"):
            with self.assertRaises(Refused, msg=bad) as caught:
                clean_details(bad)
            self.assertEqual(caught.exception.code, "invalid_account_details")


class FamilyOwnedAccountTests(ReceivablesTestCase):
    """The account is the family's. It does not depend on how many children there are or which of them owes."""

    def setUp(self):
        super().setUp()
        self.bello_family()
        self.publish_bello_fees()

    def test_a_family_of_three_children_has_one_account_and_it_is_not_any_childs(self):
        account = collection_accounts.register(self.family, provider="gtbank", account_number="0123456789", account_name="BRIGHTGATE / BELLO", actor=self.owner)
        self.assertEqual(account.family, self.family)
        for child in (self.ahmad, self.aisha, self.maryam):
            self.assertNotEqual(account.account_number, str(child.id))
            self.assertNotIn(child.student_code, account.account_number)
        self.assertEqual(FamilyCollectionAccount.objects.filter(family=self.family).count(), 1)
        self.assertEqual(account.status, AccountStatus.ACTIVE)  # the family owes, so it is live

    def test_the_same_account_serves_whichever_child_owes(self):
        account = collection_accounts.register(self.family, provider="gtbank", account_number="0123456789", actor=self.owner)
        for child in (self.ahmad, self.aisha, self.maryam):
            self.assertEqual(statements.collection_account_facts(families.family_of(child))[0]["id"], str(account.id))
        # Two of the three children settle up; the account carries on for the one who still owes.
        allocation.allocate(self.payment(200_000 * N), self.family)
        account.refresh_from_db()
        self.assertEqual((account.status, ledger.family_position(self.family).outstanding), (AccountStatus.ACTIVE, 100_000 * N))

    def test_a_family_has_at_most_one_live_account_per_school_whatever_the_provider(self):
        collection_accounts.register(self.family, provider="gtbank", account_number="0123456789", actor=self.owner)
        for provider, number in (("gtbank", "0123456780"), ("moniepoint", "MP-4471-2209")):
            with self.assertRaises(Refused) as caught:
                collection_accounts.register(self.family, provider=provider, account_number=number, actor=self.owner)
            self.assertEqual(caught.exception.code, "account_exists")
        self.assertEqual(FamilyCollectionAccount.objects.filter(family=self.family).count(), 1)

    def test_the_database_itself_refuses_a_second_live_account(self):
        collection_accounts.register(self.family, provider="gtbank", account_number="0123456789", actor=self.owner)
        with self.assertRaises(IntegrityError), transaction.atomic():
            FamilyCollectionAccount.objects.create(
                school=self.school, family=self.family, provider="monnify", account_number="9999999999", origin="legacy_manual"
            )

    def test_a_closed_account_is_history_and_the_family_can_be_given_another(self):
        first = collection_accounts.register(self.family, provider="gtbank", account_number="0123456789", actor=self.owner)
        collection_accounts.close(first, actor=self.owner, reason="Term over")
        second = collection_accounts.register(self.family, provider="monnify", account_number="9999999999", actor=self.owner)
        history = list(FamilyCollectionAccount.objects.filter(family=self.family).order_by("created_at"))
        self.assertEqual([a.status for a in history], [AccountStatus.CLOSED, AccountStatus.ACTIVE])
        self.assertIsNotNone(history[0].closed_at)
        self.assertEqual(history[0].close_reason, "Term over")
        self.assertEqual(collection_accounts.live_account(self.family), second)

    def test_a_different_school_gets_its_own_independent_account_for_the_same_kind_of_family(self):
        collection_accounts.register(self.family, provider="gtbank", account_number="0123456789", actor=self.owner)
        other_student = self.make_student("Zed", "Bello", school=self.other_school)
        other_family = families.create_family(self.other_school, display_name="Bello family", actor=self.other_owner, students=[other_student])
        theirs = collection_accounts.register(other_family, provider="gtbank", account_number="0123456789", actor=self.other_owner)
        self.assertNotEqual(theirs.family, self.family)
        self.assertEqual(FamilyCollectionAccount.objects.count(), 2)

    def test_an_account_can_be_recorded_with_a_provider_reference_and_no_number(self):
        account = collection_accounts.register(self.family, provider="other-bank", external_account_ref="ref-7781", actor=self.owner)
        self.assertEqual((account.account_number, account.external_account_ref), ("", "ref-7781"))

    def test_a_number_that_cannot_be_a_number_is_refused_at_the_door(self):
        with self.assertRaises(Refused) as caught:
            collection_accounts.register(self.family, provider="gtbank", account_number="#!", actor=self.owner)
        self.assertEqual(caught.exception.code, "invalid_account_number")
        self.assertFalse(FamilyCollectionAccount.objects.exists())

    def test_only_the_owner_or_a_provider_manager_may_record_one_by_hand(self):
        for role in ("teacher", "accountant", "principal"):
            with self.assertRaises(Refused, msg=role):
                collection_accounts.register(self.family, provider="gtbank", account_number="0123456789", actor=self.members[role])


class WhatAParentSeesTests(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.bello_family()
        self.publish_bello_fees()

    def facts(self):
        return statements.collection_account_facts(self.family)

    def test_the_facts_carry_what_the_provider_calls_the_number_and_what_to_tell_the_payer(self):
        collection_accounts.register(self.family, provider="paystack", bank_name="Wema Bank", account_number="0123456789", account_name="BRIGHTGATE / BELLO", actor=self.owner)
        (fact,) = self.facts()
        self.assertEqual((fact["numberLabel"], fact["accountNumber"], fact["canPay"], fact["isTest"]), ("Account number", "0123456789", True, False))
        self.assertIn("bank transfer", fact["note"])
        self.assertEqual(set(fact), {"id", "provider", "bankName", "accountName", "status", "accountNumber", "numberLabel", "details", "note", "canPay", "isTest"})

    def test_an_account_being_set_up_or_paused_is_listed_but_its_number_is_withheld(self):
        setting_up = collection_accounts.register(self.family, provider="gtbank", account_number="0123456789", actor=self.owner, provisioned=False)
        (fact,) = self.facts()
        self.assertEqual((fact["status"], fact["accountNumber"], fact["canPay"], fact["details"]), ("provisioning", "", False, []))
        collection_accounts.mark_provisioned(setting_up, actor=self.owner)
        self.assertEqual(self.facts()[0]["accountNumber"], "0123456789")
        collection_accounts.suspend(setting_up, actor=self.owner, reason="Under review")
        (fact,) = self.facts()
        self.assertEqual((fact["status"], fact["accountNumber"], fact["canPay"]), ("suspended", "", False))

    def test_a_closed_account_is_not_offered_and_a_dormant_one_still_is(self):
        old = collection_accounts.register(self.family, provider="gtbank", account_number="0123456789", actor=self.owner)
        collection_accounts.close(old, actor=self.owner, reason="Replaced")
        self.assertEqual(self.facts(), [])
        collection_accounts.register(self.family, provider="gtbank", account_number="0123456700", actor=self.owner)
        allocation.allocate(self.payment(300_000 * N), self.family)  # everything paid: the account rests
        (fact,) = self.facts()
        self.assertEqual((fact["status"], fact["accountNumber"], fact["canPay"]), ("dormant", "0123456700", True))

    def test_staff_get_the_details_too_but_never_the_provider_internals(self):
        account = collection_accounts.register(
            self.family, provider="gtbank", account_number="0123456789", actor=self.owner, provider_meta={"apiKey": "SECRET"},
            public_details=[{"label": "Payment reference", "value": "BG-1"}],
        )
        from .. import serializers

        shown = serializers.account(account)
        self.assertEqual((shown["details"], shown["numberLabel"], shown["origin"]), ([{"label": "Payment reference", "value": "BG-1"}], "Account number", "legacy_manual"))
        self.assertNotIn("SECRET", json.dumps(shown))
        self.assertNotIn("providerMeta", shown)


class ParentEndpointTests(test_api.ApiTestCase):
    def setUp(self):
        super().setUp()
        self.with_fees()
        self.parent = self.members["parent"]
        GuardianLink.objects.filter(student__in=[self.ahmad, self.aisha, self.maryam]).update(account_user=self.parent.user)
        collection_accounts.register(
            self.family, provider="paystack", bank_name="Wema Bank", account_number="0123456789", account_name="BELLO",
            public_details=[{"label": "Payment reference", "value": "BG-0042"}], actor=self.owner, provider_meta={"apiKey": "SECRET"},
        )

    def test_a_parent_is_shown_the_familys_account_once_with_the_extra_facts(self):
        families_ = self.get("me/families/", who=self.parent).json()["families"]
        self.assertEqual(len(families_), 1)  # three children, one family, one place to pay
        (account,) = families_[0]["collectionAccounts"]
        self.assertEqual((account["accountNumber"], account["numberLabel"], account["details"]), ("0123456789", "Account number", [{"label": "Payment reference", "value": "BG-0042"}]))
        statement = self.get(f"me/families/{self.family.id}/statement/", who=self.parent).json()["statement"]
        self.assertEqual(statement["collectionAccounts"], [account])
        self.assertNotIn("SECRET", json.dumps([families_, statement]))

    def test_a_parent_of_a_family_with_no_account_yet_is_told_there_is_none_rather_than_shown_a_childs_id(self):
        FamilyCollectionAccount.objects.all().update(status="closed")
        family = self.get("me/families/", who=self.parent).json()["families"][0]
        self.assertEqual(family["collectionAccounts"], [])
        for child in (self.ahmad, self.aisha, self.maryam):
            self.assertNotIn(str(child.id), json.dumps(family["collectionAccounts"]))


class StatementVoidTests(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.bello_family()
        self.publish_bello_fees()
        self.statement = statements.issue(self.family, session=self.session, actor=self.owner)

    def finance_actor(self):
        return self.members["accountant"]

    def test_a_statement_issued_in_error_is_voided_with_who_and_why_and_stays_on_record(self):
        before = ledger.family_position(self.family)
        voided = statements.void(self.statement, actor=self.finance_actor(), reason="Issued to the wrong term")
        self.assertEqual(voided.status, "void")
        self.assertEqual((voided.void_reason, voided.voided_by), ("Issued to the wrong term", self.finance_actor()))
        self.assertIsNotNone(voided.voided_at)
        self.assertEqual(FamilyStatement.objects.filter(family=self.family).count(), 1)  # never deleted
        self.assertEqual(ledger.family_position(self.family), before)  # what the family owes is untouched
        self.assertTrue(FinanceAuditEvent.objects.filter(school=self.school, kind="statement_voided").exists())

    def test_a_reason_is_required_and_it_can_only_be_voided_once(self):
        for reason in ("", "   ", None):
            with self.assertRaises(Refused) as caught:
                statements.void(self.statement, actor=self.owner, reason=reason)
            self.assertEqual(caught.exception.code, "reason_required")
        with self.assertRaises(Refused) as caught:
            statements.void(self.statement, actor=self.owner, reason="x" * 301)
        self.assertEqual(caught.exception.code, "reason_too_long")
        statements.void(self.statement, actor=self.owner, reason="Wrong term")
        with self.assertRaises(Refused) as caught:
            statements.void(self.statement, actor=self.owner, reason="Again")
        self.assertEqual(caught.exception.code, "already_void")

    def test_a_replacement_gets_a_new_number_and_the_old_one_is_not_reused(self):
        statements.void(self.statement, actor=self.owner, reason="Wrong term")
        second = statements.issue(self.family, session=self.session, actor=self.owner)
        self.assertNotEqual(second.number, self.statement.number)
        self.assertEqual(FamilyStatement.objects.filter(family=self.family, status="void").count(), 1)

    def test_only_the_finance_side_may_void(self):
        for role in ("teacher", "parent", "student"):
            with self.assertRaises(Refused, msg=role):
                statements.void(self.statement, actor=self.members[role], reason="No")


class FamilyAccountApiTests(test_api.ApiTestCase):
    def setUp(self):
        super().setUp()
        self.with_fees()

    def test_the_family_list_can_show_each_familys_accounts_and_find_those_without_one(self):
        collection_accounts.register(self.family, provider="gtbank", account_number="0123456789", actor=self.owner)
        other = families.create_family(self.school, display_name="Sani family", actor=self.owner, students=[self.make_student("Y", "Sani")])
        everyone = self.get("families/?withAccounts=1").json()
        rows = {f["id"]: f for f in everyone["families"]}
        self.assertEqual([a["accountNumber"] for a in rows[str(self.family.id)]["collectionAccounts"]], ["0123456789"])
        self.assertEqual(rows[str(other.id)]["collectionAccounts"], [])
        self.assertEqual(len(rows[str(self.family.id)]["students"]), 3)
        without = self.get("families/?accounts=without").json()["families"]
        self.assertEqual([f["id"] for f in without], [str(other.id)])
        self.assertEqual([f["id"] for f in self.get("families/?accounts=with").json()["families"]], [str(self.family.id)])
        self.assertNotIn("collectionAccounts", self.get("families/").json()["families"][0])  # plain listing stays light

    def test_the_family_list_says_whether_this_person_may_decide_billing_so_a_screen_offers_only_what_is_accepted(self):
        self.assertTrue(self.get("families/", who=self.owner).json()["permissions"]["canDecideBilling"])
        self.assertFalse(self.get("families/", who=self.finance).json()["permissions"]["canDecideBilling"])
        self.assertTrue(self.get("families/", who=self.authority).json()["permissions"]["canDecideBilling"])

    def test_the_family_list_is_paged(self):
        for i in range(5):
            families.create_family(self.school, display_name=f"Family {i}", actor=self.owner)
        page = self.get("families/?limit=2").json()
        self.assertEqual((len(page["families"]), page["hasMore"]), (2, True))
        last = self.get("families/?limit=2&offset=4").json()
        self.assertEqual((len(last["families"]), last["hasMore"]), (2, False))

    def test_a_statement_is_voided_over_the_api_with_a_reason(self):
        statement = statements.issue(self.family, session=self.session, actor=self.owner)
        self.assertEqual(self.post(f"statements/{statement.id}/void/", {}).json()["code"], "reason_required")
        voided = self.post(f"statements/{statement.id}/void/", {"reason": "Wrong term"}).json()["statement"]
        self.assertEqual((voided["status"], voided["voidReason"]), ("void", "Wrong term"))
        listed = self.get(f"families/{self.family.id}/statements/").json()["statements"]
        self.assertEqual([s["status"] for s in listed], ["void"])
        self.assertEqual(self.post(f"statements/{statement.id}/void/", {"reason": "Wrong term"}, who=self.members["teacher"]).status_code, 403)
        self.assertEqual(self.post(f"statements/{statement.id}/void/", {"reason": "x"}, who=self.other_owner, school=self.other_school).status_code, 404)

    def test_the_old_ways_of_making_an_account_are_gone_from_the_normal_api(self):
        for tail in ("collection-accounts/providers/", "collection-accounts/issue-missing/", f"families/{self.family.id}/collection-accounts/issue/"):
            self.assertEqual(self.post(tail, {}).status_code, 404, tail)
        self.assertEqual(self.post(f"families/{self.family.id}/collection-accounts/", {"provider": "monnify", "accountNumber": "12345"}).status_code, 405)
