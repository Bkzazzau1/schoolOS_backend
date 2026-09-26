"""A family's payment account: one per family (never per child), shaped by the bank that issued it, shown to the
parent, and issued by a provider adapter where one can.

School fees are the school's own money: every account here is the school's own provider account under one of its
connections. Nothing in this file (or the code it tests) involves SchoolOS's own income, which is `apps.billing`.
"""

import json
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.bankconnect import sandbox_tools
from apps.bankconnect.identifiers import hash_token
from apps.bankconnect.models import BankConnection, BankTransaction
from apps.bankconnect.providers.sandbox import SIGNATURE_HEADER, SandboxConnector
from apps.bankconnect.vault import context_for, get_vault
from apps.students.models import GuardianLink

from .. import account_shapes, allocation, collection_accounts, families, issuers, ledger, statements
from ..account_shapes import AccountShape, clean_details
from ..errors import Refused
from ..models import AccountStatus, FamilyCollectionAccount, FamilyStatement, FinanceAuditEvent
from . import test_api
from .base import ReceivablesTestCase

N = 100
TOKEN = "a-long-random-webhook-token-for-issued-accounts"
SECRET = "webhook-signing-secret"
User = get_user_model()


class ShapeTests(ReceivablesTestCase):
    def test_the_default_shape_does_not_assume_ten_digits(self):
        shape = account_shapes.GENERIC
        for number in ("0123456789", "8012345678", "WEMA-4471-2209", "FAM/K7Q2/01", "PAY4471"):
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

    def test_an_unknown_or_hand_typed_provider_gets_the_default_and_a_listed_one_its_own(self):
        self.assertIs(issuers.shape_for("some-microfinance-bank"), account_shapes.GENERIC)
        self.assertIs(issuers.shape_for("gtbank"), account_shapes.GENERIC)  # listed, but no verified format to narrow it
        with override_settings(BANKCONNECT_ENABLE_SANDBOX=True):
            self.assertEqual(issuers.shape_for("sandbox").number_label, "Test account number")

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

    def test_a_family_may_hold_accounts_with_several_banks_each_shaped_its_own_way(self):
        collection_accounts.register(self.family, provider="gtbank", bank_name="GTBank", account_number="0123456789", actor=self.owner)
        collection_accounts.register(
            self.family, provider="moniepoint", bank_name="Moniepoint", account_number="MP-4471-2209", account_name="BELLO FAMILY",
            public_details=[{"label": "Payment reference", "value": "BG-0042"}, {"label": "Sort code", "value": "090405"}], actor=self.owner,
        )
        facts = statements.collection_account_facts(self.family)
        self.assertEqual({f["provider"] for f in facts}, {"gtbank", "moniepoint"})
        moniepoint = next(f for f in facts if f["provider"] == "moniepoint")
        self.assertEqual(moniepoint["accountNumber"], "MP-4471-2209")
        self.assertEqual(moniepoint["details"], [{"label": "Payment reference", "value": "BG-0042"}, {"label": "Sort code", "value": "090405"}])

    def test_but_only_one_live_account_per_bank_and_a_number_is_never_shared(self):
        collection_accounts.register(self.family, provider="gtbank", account_number="0123456789", actor=self.owner)
        with self.assertRaises(Refused) as caught:
            collection_accounts.register(self.family, provider="gtbank", account_number="0123456780", actor=self.owner)
        self.assertEqual(caught.exception.code, "account_exists")
        other = families.create_family(self.school, display_name="Sani family", actor=self.owner)
        with self.assertRaises(Refused) as caught:
            collection_accounts.register(other, provider="gtbank", account_number="0123456789", actor=self.owner)
        self.assertEqual(caught.exception.code, "identifier_in_use")

    def test_an_account_can_be_recorded_with_a_provider_reference_and_no_number(self):
        account = collection_accounts.register(self.family, provider="other-bank", external_account_ref="ref-7781", actor=self.owner)
        self.assertEqual((account.account_number, account.external_account_ref), ("", "ref-7781"))

    def test_a_number_that_cannot_be_the_providers_is_refused_at_the_door(self):
        with self.assertRaises(Refused) as caught:
            collection_accounts.register(self.family, provider="gtbank", account_number="#!", actor=self.owner)
        self.assertEqual(caught.exception.code, "invalid_account_number")
        self.assertFalse(FamilyCollectionAccount.objects.exists())

    def test_only_the_school_finance_side_may_record_one(self):
        with self.assertRaises(Refused):
            collection_accounts.register(self.family, provider="gtbank", account_number="0123456789", actor=self.members["teacher"])


class WhatAParentSeesTests(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.bello_family()
        self.publish_bello_fees()

    def facts(self):
        return statements.collection_account_facts(self.family)

    def test_the_facts_carry_what_the_provider_calls_the_number_and_what_to_tell_the_payer(self):
        collection_accounts.register(self.family, provider="gtbank", bank_name="GTBank", account_number="0123456789", account_name="BRIGHTGATE / BELLO", actor=self.owner)
        (fact,) = self.facts()
        self.assertEqual((fact["numberLabel"], fact["accountNumber"], fact["canPay"], fact["isTest"]), ("Account number", "0123456789", True, False))
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

    def test_live_accounts_come_first(self):
        collection_accounts.register(self.family, provider="uba", bank_name="UBA", account_number="1000000001", actor=self.owner, provisioned=False)
        collection_accounts.register(self.family, provider="gtbank", bank_name="GTBank", account_number="0123456789", actor=self.owner)
        self.assertEqual([f["provider"] for f in self.facts()], ["gtbank", "uba"])

    def test_staff_get_the_details_too_but_never_the_provider_internals(self):
        account = collection_accounts.register(
            self.family, provider="gtbank", account_number="0123456789", actor=self.owner, provider_meta={"apiKey": "SECRET"},
            public_details=[{"label": "Payment reference", "value": "BG-1"}],
        )
        from .. import serializers

        shown = serializers.account(account)
        self.assertEqual((shown["details"], shown["numberLabel"]), ([{"label": "Payment reference", "value": "BG-1"}], "Account number"))
        self.assertNotIn("SECRET", json.dumps(shown))
        self.assertNotIn("providerMeta", shown)


class ParentEndpointTests(test_api.ApiTestCase):
    def setUp(self):
        super().setUp()
        self.with_fees()
        self.parent = self.members["parent"]
        GuardianLink.objects.filter(student__in=[self.ahmad, self.aisha, self.maryam]).update(account_user=self.parent.user)
        collection_accounts.register(
            self.family, provider="moniepoint", bank_name="Moniepoint", account_number="MP-4471-2209", account_name="BELLO",
            public_details=[{"label": "Payment reference", "value": "BG-0042"}], actor=self.owner, provider_meta={"apiKey": "SECRET"},
        )

    def test_a_parent_is_shown_the_familys_account_once_with_the_extra_facts(self):
        families_ = self.get("me/families/", who=self.parent).json()["families"]
        self.assertEqual(len(families_), 1)  # three children, one family, one place to pay
        (account,) = families_[0]["collectionAccounts"]
        self.assertEqual((account["accountNumber"], account["numberLabel"], account["details"]), ("MP-4471-2209", "Account number", [{"label": "Payment reference", "value": "BG-0042"}]))
        statement = self.get(f"me/families/{self.family.id}/statement/", who=self.parent).json()["statement"]
        self.assertEqual(statement["collectionAccounts"], [account])
        self.assertNotIn("SECRET", json.dumps([families_, statement]))

    def test_a_parent_of_a_family_with_no_account_yet_is_told_there_is_none_rather_than_shown_a_childs_id(self):
        FamilyCollectionAccount.objects.all().update(status="closed")
        family = self.get("me/families/", who=self.parent).json()["families"][0]
        self.assertEqual(family["collectionAccounts"], [])
        for child in (self.ahmad, self.aisha, self.maryam):
            self.assertNotIn(str(child.id), json.dumps(family["collectionAccounts"]))


class IssueTests(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.bello_family()
        self.publish_bello_fees()
        self.sandbox = BankConnection.objects.create(
            school=self.school, provider="sandbox", connection_type="sandbox", bank_name="Sandbox Bank", account_name="SCHOOL",
            account_mask="****6789", purpose="tuition", status="connected", is_sandbox=True,
        )

    def with_sandbox(self):
        patcher = override_settings(BANKCONNECT_ENABLE_SANDBOX=True)
        patcher.enable()
        self.addCleanup(patcher.disable)

    def test_the_sandbox_issues_a_clearly_labelled_test_account(self):
        self.with_sandbox()
        account = collection_accounts.issue(self.family, connection=self.sandbox, actor=self.owner)
        self.assertEqual((account.family, account.provider, account.connection, account.status), (self.family, "sandbox", self.sandbox, AccountStatus.ACTIVE))
        self.assertRegex(account.account_number, r"^9\d{9}$")
        self.assertTrue(account.provider_meta["test"])
        self.assertIn({"label": "Test only", "value": "Not a real bank account"}, account.public_details)
        (fact,) = statements.collection_account_facts(self.family)
        self.assertTrue(fact["isTest"])
        self.assertTrue(FinanceAuditEvent.objects.filter(school=self.school, kind="collection_account_issued").exists())

    def test_a_family_is_issued_only_one_account_per_provider(self):
        self.with_sandbox()
        collection_accounts.issue(self.family, connection=self.sandbox, actor=self.owner)
        with self.assertRaises(Refused) as caught:
            collection_accounts.issue(self.family, connection=self.sandbox, actor=self.owner)
        self.assertEqual(caught.exception.code, "account_exists")

    def test_each_family_gets_its_own_number(self):
        self.with_sandbox()
        other = families.create_family(self.school, display_name="Sani family", actor=self.owner)
        numbers = {collection_accounts.issue(f, connection=self.sandbox, actor=self.owner).account_number for f in (self.family, other)}
        self.assertEqual(len(numbers), 2)

    def test_a_listed_bank_that_cannot_issue_yet_says_so_in_plain_words(self):
        gt = BankConnection.objects.create(
            school=self.school, provider="gtbank", connection_type="direct_bank_api", bank_name="GTBank", account_name="SCHOOL",
            account_mask="****1111", purpose="tuition", status="connected", is_sandbox=False,
        )
        with self.assertRaises(Refused) as caught:
            collection_accounts.issue(self.family, connection=gt, actor=self.owner)
        self.assertEqual(caught.exception.code, "issuer_unavailable")
        self.assertIn("verified documentation", caught.exception.message)
        self.assertIn("by hand", caught.exception.message)
        self.assertFalse(FamilyCollectionAccount.objects.exists())

    def test_the_sandbox_cannot_issue_where_it_is_switched_off(self):
        with override_settings(BANKCONNECT_ENABLE_SANDBOX=False), self.assertRaises(Refused) as caught:
            collection_accounts.issue(self.family, connection=self.sandbox, actor=self.owner)
        self.assertEqual(caught.exception.code, "issuer_unavailable")

    def test_a_connection_that_is_not_working_cannot_issue(self):
        self.with_sandbox()
        BankConnection.objects.filter(pk=self.sandbox.pk).update(status="needs_reauth")
        self.sandbox.refresh_from_db()
        with self.assertRaises(Refused) as caught:
            collection_accounts.issue(self.family, connection=self.sandbox, actor=self.owner)
        self.assertEqual(caught.exception.code, "connection_not_ready")

    def test_another_schools_connection_and_an_inactive_family_are_refused(self):
        self.with_sandbox()
        theirs = BankConnection.objects.create(
            school=self.other_school, provider="sandbox", connection_type="sandbox", bank_name="Sandbox", account_name="X", account_mask="****1",
            purpose="tuition", status="connected", is_sandbox=True,
        )
        with self.assertRaises(Refused) as caught:
            collection_accounts.issue(self.family, connection=theirs, actor=self.owner)
        self.assertEqual(caught.exception.code, "connection_not_found")
        families.set_status(self.family, "inactive", actor=self.owner)
        with self.assertRaises(Refused) as caught:
            collection_accounts.issue(self.family, connection=self.sandbox, actor=self.owner)
        self.assertEqual(caught.exception.code, "family_inactive")

    def test_only_the_finance_side_may_issue(self):
        self.with_sandbox()
        for role in ("teacher", "parent", "student"):
            with self.assertRaises(Refused, msg=role):
                collection_accounts.issue(self.family, connection=self.sandbox, actor=self.members[role])

    def test_a_number_the_provider_hands_out_twice_is_asked_for_again(self):
        self.with_sandbox()
        other = families.create_family(self.school, display_name="Sani family", actor=self.owner)
        first = collection_accounts.issue(other, connection=self.sandbox, actor=self.owner)
        real = issuers.SandboxIssuer.issue
        calls = []

        def clashing(issuer, *, family, connection, attempt=0):
            calls.append(attempt)
            return real(issuer, family=other, connection=connection, attempt=0) if attempt == 0 else real(issuer, family=family, connection=connection, attempt=attempt)

        with mock.patch.object(issuers.SandboxIssuer, "issue", clashing):
            account = collection_accounts.issue(self.family, connection=self.sandbox, actor=self.owner)
        self.assertEqual(calls, [0, 1])
        self.assertNotEqual(account.account_number, first.account_number)

    def test_issue_missing_gives_every_active_family_without_one_an_account_and_reports_the_rest(self):
        self.with_sandbox()
        collection_accounts.register(families.create_family(self.school, display_name="Already has one", actor=self.owner, students=[self.make_student("Z", "One")]), provider="sandbox", account_number="SBX-OLD-1", actor=self.owner)
        second = families.create_family(self.school, display_name="Sani family", actor=self.owner, students=[self.make_student("Y", "Sani")])
        empty = families.create_family(self.school, display_name="Nobody in it", actor=self.owner)  # no students: not offered an account
        report = collection_accounts.issue_missing(self.sandbox, actor=self.owner)
        self.assertEqual((report["issued"], report["failed"]), (2, []))
        holders = set(FamilyCollectionAccount.objects.filter(provider="sandbox").values_list("family_id", flat=True))
        self.assertIn(self.family.id, holders)
        self.assertIn(second.id, holders)
        self.assertNotIn(empty.id, holders)
        self.assertEqual(collection_accounts.issue_missing(self.sandbox, actor=self.owner)["issued"], 0)  # nothing more to do

    def test_issue_missing_for_a_bank_that_cannot_issue_is_refused_once_not_per_family(self):
        gt = BankConnection.objects.create(
            school=self.school, provider="gtbank", connection_type="direct_bank_api", bank_name="GTBank", account_name="SCHOOL",
            account_mask="****1111", purpose="tuition", status="connected", is_sandbox=False,
        )
        with self.assertRaises(Refused) as caught:
            collection_accounts.issue_missing(gt, actor=self.owner)
        self.assertEqual(caught.exception.code, "issuer_unavailable")

    def test_the_providers_listing_says_who_can_issue_and_what_each_account_looks_like(self):
        with override_settings(BANKCONNECT_ENABLE_SANDBOX=False):
            listing = {p["code"]: p for p in issuers.describe()}
        self.assertNotIn("sandbox", listing)  # not offered where it is switched off
        self.assertFalse(listing["gtbank"]["canIssue"])
        self.assertEqual(listing["gtbank"]["issuerStatus"], "pending_verified_documentation")
        self.assertEqual(listing["gtbank"]["shape"]["numberLabel"], "Account number")
        with override_settings(BANKCONNECT_ENABLE_SANDBOX=True):
            sandbox = next(p for p in issuers.describe() if p["code"] == "sandbox")
        self.assertTrue(sandbox["canIssue"])
        self.assertEqual(sandbox["shape"]["numberLabel"], "Test account number")


@override_settings(BANKCONNECT_SECRET_KEYS=[Fernet.generate_key().decode()], BANKCONNECT_ENABLE_SANDBOX=True)
class IssuedAccountReceivesMoneyTests(ReceivablesTestCase):
    """The whole path: issue an account, a payment arrives on it, and the family's charges are paid."""

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
        self.account = collection_accounts.issue(self.family, connection=self.connection, actor=self.owner)
        self.client.force_authenticate(None)

    def deliver(self, **payload):
        body = json.dumps({"transaction": sandbox_tools.transaction_payload(**payload)}).encode()
        return self.client.post(
            f"/api/v1/bank-webhooks/sandbox/{TOKEN}/", data=body, content_type="application/json",
            **{"HTTP_" + SIGNATURE_HEADER.upper().replace("-", "_"): SandboxConnector.sign(body, SECRET)},
        )

    def test_a_payment_into_the_issued_account_pays_the_family_not_a_particular_child(self):
        response = self.deliver(external_transaction_id="I1", amount_minor=150_000 * N, receiving_account_reference=self.account.account_number, sender_name="A cousin")
        self.assertEqual(response.json()["outcome"], "processed")
        tx = BankTransaction.objects.get()
        self.assertEqual((tx.family, tx.reconciliation_status), (self.family, "matched"))
        self.assertEqual(ledger.family_position(self.family).outstanding, 150_000 * N)
        self.assertLedgerHolds()
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, AccountStatus.ACTIVE)  # still owes, so still live

    def test_paying_it_all_rests_the_account_without_closing_it(self):
        self.deliver(external_transaction_id="I2", amount_minor=300_000 * N, receiving_account_reference=self.account.account_number)
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, AccountStatus.DORMANT)
        (fact,) = statements.collection_account_facts(self.family)
        self.assertEqual((fact["accountNumber"], fact["canPay"]), (self.account.account_number, True))


class StatementVoidTests(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.bello_family()
        self.publish_bello_fees()
        self.statement = statements.issue(self.family, session=self.session, actor=self.owner)

    def test_a_statement_issued_in_error_is_voided_with_who_and_why_and_stays_on_record(self):
        before = ledger.family_position(self.family)
        voided = statements.void(self.statement, actor=self.finance_actor(), reason="Issued to the wrong term")
        self.assertEqual(voided.status, "void")
        self.assertEqual((voided.void_reason, voided.voided_by), ("Issued to the wrong term", self.finance_actor()))
        self.assertIsNotNone(voided.voided_at)
        self.assertEqual(FamilyStatement.objects.filter(family=self.family).count(), 1)  # never deleted
        self.assertEqual(ledger.family_position(self.family), before)  # what the family owes is untouched
        self.assertTrue(FinanceAuditEvent.objects.filter(school=self.school, kind="statement_voided").exists())

    def finance_actor(self):
        return self.members["accountant"]

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
        self.sandbox = BankConnection.objects.create(
            school=self.school, provider="sandbox", connection_type="sandbox", bank_name="Sandbox Bank", account_name="SCHOOL",
            account_mask="****6789", purpose="tuition", status="connected", is_sandbox=True,
        )

    def test_the_providers_endpoint_is_for_the_finance_side_only(self):
        response = self.get("collection-accounts/providers/")
        self.assertEqual(response.status_code, 200)
        codes = {p["code"] for p in response.json()["providers"]}
        self.assertLessEqual({"gtbank", "moniepoint", "paystack"}, codes)
        for role in ("teacher", "parent", "student"):
            self.assertEqual(self.get("collection-accounts/providers/", who=self.members[role]).status_code, 403, role)

    @override_settings(BANKCONNECT_ENABLE_SANDBOX=True)
    def test_issue_and_issue_missing_work_over_the_api(self):
        made = self.post(f"families/{self.family.id}/collection-accounts/issue/", {"connectionId": str(self.sandbox.id)})
        self.assertEqual(made.status_code, 201)
        account = made.json()["account"]
        self.assertEqual((account["provider"], account["isTest"], account["numberLabel"]), ("sandbox", True, "Test account number"))
        again = self.post(f"families/{self.family.id}/collection-accounts/issue/", {"connectionId": str(self.sandbox.id)})
        self.assertEqual((again.status_code, again.json()["code"]), (400, "account_exists"))
        other = families.create_family(self.school, display_name="Sani family", actor=self.owner, students=[self.make_student("Y", "Sani")])
        report = self.post("collection-accounts/issue-missing/", {"connectionId": str(self.sandbox.id)}).json()
        self.assertEqual((report["issued"], report["failed"]), (1, []))
        self.assertTrue(FamilyCollectionAccount.objects.filter(family=other).exists())

    def test_a_connection_is_required_and_must_be_the_schools_own(self):
        self.assertEqual(self.post(f"families/{self.family.id}/collection-accounts/issue/", {}).json()["code"], "connection_required")
        theirs = BankConnection.objects.create(
            school=self.other_school, provider="sandbox", connection_type="sandbox", bank_name="S", account_name="X", account_mask="****1",
            purpose="tuition", status="connected", is_sandbox=True,
        )
        self.assertEqual(self.post("collection-accounts/issue-missing/", {"connectionId": str(theirs.id)}).status_code, 404)

    def test_a_bank_that_cannot_issue_answers_with_its_reason(self):
        gt = BankConnection.objects.create(
            school=self.school, provider="gtbank", connection_type="direct_bank_api", bank_name="GTBank", account_name="SCHOOL",
            account_mask="****1111", purpose="tuition", status="connected", is_sandbox=False,
        )
        response = self.post(f"families/{self.family.id}/collection-accounts/issue/", {"connectionId": str(gt.id)})
        self.assertEqual((response.status_code, response.json()["code"]), (400, "issuer_unavailable"))

    def test_an_account_recorded_by_hand_takes_any_banks_shape_and_its_extra_facts(self):
        response = self.post(f"families/{self.family.id}/collection-accounts/", {
            "provider": "moniepoint", "bankName": "Moniepoint", "accountNumber": "MP 4471 2209", "accountName": "BELLO",
            "details": [{"label": "Payment reference", "value": "BG-0042"}],
        })
        self.assertEqual(response.status_code, 201)
        account = response.json()["account"]
        self.assertEqual((account["accountNumber"], account["details"]), ("MP44712209", [{"label": "Payment reference", "value": "BG-0042"}]))
        bad = self.post(f"families/{self.family.id}/collection-accounts/", {"provider": "gtbank", "accountNumber": "#!"})
        self.assertEqual((bad.status_code, bad.json()["code"]), (400, "invalid_account_number"))

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
