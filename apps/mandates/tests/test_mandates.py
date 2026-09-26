"""A payer's mandate: started by staff, authorised by the payer themselves, activated with the provider, and debit-ready only when the provider says so."""

from dataclasses import replace
from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound

from .. import consent, mandate_provider, mandates
from ..constants import ConsentChannel, MandateStatus
from ..errors import MandateRefused
from ..models import DirectDebitMandate, MandateConsent, SandboxMandate
from ..providers import sandbox
from ..providers.sandbox import SandboxMandateConnector
from .base import ACCOUNT, MANAGE, PROVIDER, MandateTestCase, sealed_of


class StartingTests(MandateTestCase):
    def setUp(self):
        super().setUp()
        self.family = self.make_family("Bello")

    def test_a_mandate_is_started_for_a_payer_and_the_account_is_only_ever_masked(self):
        mandate = self.start_mandate(self.family)
        self.assertEqual((mandate.family_id, mandate.payer.guardian.name, mandate.provider), (self.family.id, "Bello Parent", "sandbox"))
        self.assertEqual((mandate.bank_code, mandate.account_mask, mandate.status), ("058", "****6789", MandateStatus.PENDING_ACTIVATION))
        self.assertEqual(len(mandate.account_fingerprint), 64)
        self.assertNotIn(ACCOUNT, mandate.account_fingerprint)
        stored = " ".join(str(getattr(mandate, f.name)) for f in mandate._meta.fields if f.name != "sealed_account_details")
        self.assertNotIn(ACCOUNT, stored)  # no readable column has the account number

    def test_the_full_account_number_is_sealed_and_bound_to_its_mandate(self):
        from ..vault import VaultError, account_context_for, get_vault

        mandate = self.start_mandate(self.family, route="provider")
        sealed = DirectDebitMandate.objects.get(pk=mandate.pk).sealed_account_details
        self.assertTrue(sealed)  # kept: this provider stand-in still needs it in the test below
        with self.assertRaises(VaultError):
            get_vault().open(account_context_for(type("M", (), {"school_id": mandate.school_id, "id": "someone-else"})()), bytes(sealed))
        self.assertEqual(get_vault().open(account_context_for(mandate), bytes(sealed))["account_number"], ACCOUNT)
        self.assertNotIn(ACCOUNT.encode(), bytes(sealed))

    def test_only_someone_with_the_manage_duty_can_start_one(self):
        for who in (self.maker, self.checker, self.members["teacher"]):
            with self.assertRaises(MandateRefused) as caught:
                self.start_mandate(self.family, who=who)
            self.assertEqual(caught.exception.code, "not_mandate_manager")
        self.assertEqual(DirectDebitMandate.objects.count(), 0)
        self.assertEqual(self.start_mandate(self.family, who=self.owner).status, MandateStatus.PENDING_ACTIVATION)  # the owner always may

    def test_being_an_accountant_or_holding_a_collection_duty_is_not_enough(self):
        self.give_duties(self.members["accountant"], "finance.collection_provider_manage", "finance.collection_prepare", "finance.billing_authority")
        with self.assertRaises(MandateRefused):
            self.start_mandate(self.family, who=self.members["accountant"])

    def test_bad_input_is_refused(self):
        payer = self.payer_of(self.family)
        other = self.make_family("Sani")
        cases = [
            ({"bank": "1"}, "invalid_bank"), ({"account": "12345"}, "invalid_account_number"), ({"account": "01234567AB"}, "invalid_account_number"),
            ({"maximum": 0}, "invalid_maximum"),
        ]
        for kw, code in cases:
            with self.assertRaises(MandateRefused) as caught:
                self.start_mandate(self.family, **kw)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(MandateRefused) as caught:  # a payer of another family
            mandates.start(self.manager, family_id=self.family.id, payer_id=self.payer_of(other).id, connection_id=self.connection.id, bank_code="058", account_number=ACCOUNT, maximum_amount_minor=1)
        self.assertEqual(caught.exception.code, "payer_not_found")
        with self.assertRaises(MandateRefused) as caught:
            self.start_mandate(self.family, end_date=timezone.localdate() - timedelta(days=1))
        self.assertEqual(caught.exception.code, "invalid_dates")
        self.assertIsNotNone(payer)

    def test_a_second_live_mandate_on_the_same_account_is_refused_but_a_cancelled_one_does_not_block(self):
        first = self.start_mandate(self.family)
        other = self.make_family("Sani")
        with self.assertRaises(MandateRefused) as caught:
            self.start_mandate(other, account=ACCOUNT)
        self.assertEqual(caught.exception.code, "mandate_exists")
        mandate_provider.cancel(self.manager, first.id, reason="Closed the account")
        self.assertEqual(self.start_mandate(other, account=ACCOUNT).status, MandateStatus.PENDING_ACTIVATION)

    def test_the_first_live_mandate_is_primary_and_a_family_may_have_more_than_one(self):
        first = self.start_mandate(self.family)
        second = self.start_mandate(self.family, account="0987654321")
        self.assertEqual((first.is_primary, second.is_primary), (True, False))
        mandates.set_primary(self.manager, second.id)
        self.assertEqual([self.reload(first).is_primary, self.reload(second).is_primary], [False, True])
        with self.assertRaises(IntegrityError), transaction.atomic():
            DirectDebitMandate.objects.filter(pk=first.pk).update(is_primary=True)  # the database allows one primary per family

    def test_an_ended_mandate_can_never_be_primary(self):
        first = self.start_mandate(self.family)
        mandate_provider.cancel(self.manager, first.id)
        self.assertFalse(self.reload(first).is_primary)
        with self.assertRaises(MandateRefused):
            mandates.set_primary(self.manager, first.id)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DirectDebitMandate.objects.filter(pk=first.pk).update(is_primary=True)

    def test_the_provider_reference_is_unique_on_a_connection(self):
        first = self.start_mandate(self.family)
        other = self.make_family("Sani")
        second = self.start_mandate(other, account="0987654321")
        with self.assertRaises(IntegrityError), transaction.atomic():
            DirectDebitMandate.objects.filter(pk=second.pk).update(provider_mandate_reference=first.provider_mandate_reference)

    def test_another_schools_mandate_answers_404(self):
        mandate = self.start_mandate(self.family)
        with self.assertRaises(NotFound):
            mandates.get_mandate(self.other_owner, mandate.id)

    def test_a_payer_details_a_provider_needs_must_be_on_record(self):
        family = self.make_family("Noemail", email=None)
        connector = SandboxMandateConnector()
        with mock.patch.object(type(connector), "info", replace(connector.info, payer_requirements=("name", "email"))):
            with self.assertRaises(MandateRefused) as caught:
                self.start_mandate(family)
        self.assertEqual((caught.exception.code, caught.exception.extra["missing"]), ("payer_details_missing", ["email"]))


class ConsentTests(MandateTestCase):
    def setUp(self):
        super().setUp()
        self.family = self.make_family("Bello")
        self.parent = self.make_parent(self.family)

    def start_app_mandate(self):
        return self.start_mandate(self.family, route="payer_app")

    def hash_shown(self, mandate):
        return consent.text_hash(consent.consent_text(self.reload(mandate)))

    def test_an_app_mandate_waits_for_the_payer_and_nothing_reaches_the_provider(self):
        mandate = self.start_app_mandate()
        self.assertEqual(mandate.status, MandateStatus.PENDING_CONSENT)
        self.assertFalse(SandboxMandate.objects.exists())  # the provider has not been asked for anything
        self.assertEqual(mandate.provider_mandate_reference, "")
        self.assertIsNone(mandate.consent_at)

    def test_the_payer_authorises_it_themselves_and_only_then_is_the_provider_asked(self):
        mandate = self.start_app_mandate()
        done = mandate_provider.payer_consent(self.parent, mandate.id, shown_hash=self.hash_shown(mandate), accepted=True)
        self.assertEqual(done.status, MandateStatus.PENDING_ACTIVATION)
        self.assertTrue(SandboxMandate.objects.filter(provider_ref=done.provider_mandate_reference).exists())
        record = MandateConsent.objects.get(mandate=mandate)
        self.assertEqual((record.channel, record.consent_version, record.recorded_by_id), (ConsentChannel.PAYER_APP, consent.CONSENT_VERSION, self.parent.id))
        self.assertEqual(len(record.consent_text_hash), 64)
        self.assertIsNotNone(self.reload(mandate).consent_at)

    def test_staff_cannot_give_consent_for_the_payer(self):
        mandate = self.start_app_mandate()
        for who in (self.owner, self.manager, self.maker, self.checker):
            with self.assertRaises(NotFound):
                mandate_provider.payer_consent(who, mandate.id, shown_hash=self.hash_shown(mandate), accepted=True)
        self.assertFalse(MandateConsent.objects.exists())
        self.assertEqual(self.reload(mandate).status, MandateStatus.PENDING_CONSENT)

    def test_another_parent_cannot_authorise_a_mandate_that_is_not_theirs(self):
        mandate = self.start_app_mandate()
        other_family = self.make_family("Sani")
        other_parent = self.make_parent(other_family)
        with self.assertRaises(NotFound):
            mandate_provider.payer_consent(other_parent, mandate.id, shown_hash=self.hash_shown(mandate), accepted=True)

    def test_consent_needs_an_explicit_yes_and_the_words_the_payer_actually_saw(self):
        mandate = self.start_app_mandate()
        with self.assertRaises(MandateRefused) as caught:
            mandate_provider.payer_consent(self.parent, mandate.id, shown_hash=self.hash_shown(mandate), accepted=False)
        self.assertEqual(caught.exception.code, "consent_not_given")
        with self.assertRaises(MandateRefused) as caught:
            mandate_provider.payer_consent(self.parent, mandate.id, shown_hash="0" * 64, accepted=True)
        self.assertEqual(caught.exception.code, "stale_consent")
        self.assertFalse(MandateConsent.objects.exists())

    def test_consent_cannot_be_given_twice_or_after_the_mandate_moved_on(self):
        mandate = self.start_app_mandate()
        mandate_provider.payer_consent(self.parent, mandate.id, shown_hash=self.hash_shown(mandate), accepted=True)
        with self.assertRaises(MandateRefused) as caught:
            mandate_provider.payer_consent(self.parent, mandate.id, shown_hash=self.hash_shown(mandate), accepted=True)
        self.assertEqual(caught.exception.code, "not_pending_consent")

    def test_the_provider_is_never_asked_for_an_app_mandate_without_consent(self):
        mandate = self.start_app_mandate()
        with self.assertRaises(MandateRefused) as caught:
            mandate_provider.send_to_provider(mandate.id)
        self.assertEqual(caught.exception.code, "payer_consent_required")
        self.assertFalse(SandboxMandate.objects.exists())

    def test_a_provider_route_mandate_has_no_consent_until_the_payer_activates_it_with_their_bank(self):
        mandate = self.start_mandate(self.family, route="provider")
        self.assertEqual((mandate.status, mandate.consent_at), (MandateStatus.PENDING_ACTIVATION, None))
        self.assertFalse(MandateConsent.objects.exists())
        activated = self.activate_at_provider(mandate)
        record = MandateConsent.objects.get(mandate=mandate)
        self.assertEqual((activated.status, record.channel, record.recorded_by_id), (MandateStatus.ACTIVE, ConsentChannel.PROVIDER_HOSTED, None))
        self.assertEqual(record.provider_consent_reference, mandate.provider_mandate_reference)  # the provider's reference, not a copy of the evidence

    def test_the_database_refuses_a_usable_mandate_without_consent(self):
        mandate = self.start_mandate(self.family, route="provider")
        for status in (MandateStatus.ACTIVE, MandateStatus.ACTIVATING, MandateStatus.PENDING_PROVIDER_SETUP):
            with self.assertRaises(IntegrityError), transaction.atomic():
                DirectDebitMandate.objects.filter(pk=mandate.pk).update(status=status)

    def test_a_consent_is_never_edited_or_deleted(self):
        mandate = self.start_app_mandate()
        mandate_provider.payer_consent(self.parent, mandate.id, shown_hash=self.hash_shown(mandate), accepted=True)
        record = MandateConsent.objects.get(mandate=mandate)
        record.consent_version = "99"
        with self.assertRaises(ValidationError):
            record.save()
        with self.assertRaises(ValidationError):
            record.delete()

    def test_an_app_consent_must_name_the_words_and_the_payer(self):
        mandate = self.start_app_mandate()
        with self.assertRaises(IntegrityError), transaction.atomic():
            MandateConsent.objects.create(
                school=mandate.school, mandate=mandate, payer=mandate.payer, consent_version="1", channel=ConsentChannel.PAYER_APP, consented_at=timezone.now(),
            )

    def test_a_payer_without_an_app_account_cannot_be_sent_to_the_app_route(self):
        family = self.make_family("Noapp")
        with self.assertRaises(MandateRefused) as caught:
            self.start_mandate(family, route="payer_app")
        self.assertEqual(caught.exception.code, "payer_has_no_app_account")
        self.assertEqual(self.start_mandate(family).consent_route, "provider")  # the default for such a payer

    def test_the_consent_words_say_what_it_is_and_never_show_the_account_number(self):
        mandate = self.start_app_mandate()
        text = consent.consent_text(mandate)
        for wanted in ("Bello Parent", "BrightGate", "Bello family", "6789", "direct debit", "cancel"):
            self.assertIn(wanted, text)
        self.assertNotIn(ACCOUNT, text)


class ActivationTests(MandateTestCase):
    def setUp(self):
        super().setUp()
        self.family = self.make_family("Bello")
        self.parent = self.make_parent(self.family)

    def test_the_payer_can_activate_in_the_app_with_a_bank_one_time_password(self):
        mandate = self.start_mandate(self.family, route="payer_app")
        mandate = mandate_provider.payer_consent(self.parent, mandate.id, shown_hash=consent.text_hash(consent.consent_text(mandate)), accepted=True)
        asked = mandate_provider.request_activation(self.parent, mandate.id)
        self.assertEqual(asked["fields"][0]["name"], "OTP")
        with self.assertRaises(MandateRefused) as caught:
            mandate_provider.confirm_activation(self.parent, mandate.id, answers={"OTP": "0000"})
        self.assertEqual(caught.exception.code, "activation_refused")
        self.assertEqual(self.reload(mandate).status, MandateStatus.PENDING_ACTIVATION)
        done = mandate_provider.confirm_activation(self.parent, mandate.id, answers={"OTP": "1234"})
        self.assertEqual((done.status, done.debit_ready_at is not None), (MandateStatus.ACTIVE, True))
        self.assertNotIn("challengeRef", done.activation_details)  # the challenge is used up, and what the payer typed is kept nowhere

    def test_activated_is_not_the_same_as_debit_ready(self):
        mandate = self.start_mandate(self.family)
        SandboxMandate.objects.filter(provider_ref=mandate.provider_mandate_reference).update(
            status=MandateStatus.PENDING_PROVIDER_SETUP, debit_ready_at=timezone.now() + timedelta(hours=2),
        )
        setting_up = mandate_provider.refresh_mandate(mandate.id)
        self.assertEqual(setting_up.status, MandateStatus.PENDING_PROVIDER_SETUP)
        self.assertIsNotNone(setting_up.activated_at)
        ready, why = mandates.is_debit_ready(setting_up)
        self.assertFalse(ready)
        self.assertIn("still setting it up", why)
        SandboxMandate.objects.filter(provider_ref=mandate.provider_mandate_reference).update(debit_ready_at=timezone.now() - timedelta(minutes=1))
        now_ready = mandate_provider.refresh_mandate(mandate.id)
        self.assertEqual(now_ready.status, MandateStatus.ACTIVE)
        self.assertTrue(mandates.is_debit_ready(now_ready)[0])

    def test_a_mandate_is_debit_ready_only_inside_its_dates_and_with_a_working_connection(self):
        mandate = self.active_mandate(self.family)
        self.assertTrue(mandates.is_debit_ready(mandate)[0])
        self.assertFalse(mandates.is_debit_ready(mandate, today=mandate.end_date + timedelta(days=1))[0])
        future = self.reload(mandate)
        future.start_date = timezone.localdate() + timedelta(days=5)
        self.assertFalse(mandates.is_debit_ready(future)[0])
        from ..connections import record_failure

        record_failure(mandate.provider_connection, "provider_unavailable")
        self.assertIn("connection", mandates.is_debit_ready(self.reload(mandate))[1])

    def test_a_payer_cannot_activate_someone_elses_mandate_or_ask_twice_without_a_challenge(self):
        mandate = self.start_mandate(self.family)
        other = self.make_parent(self.make_family("Sani"))
        with self.assertRaises(NotFound):
            mandate_provider.request_activation(other, mandate.id)
        with self.assertRaises(MandateRefused) as caught:
            mandate_provider.confirm_activation(self.parent, mandate.id, answers={"OTP": "1234"})
        self.assertEqual(caught.exception.code, "no_challenge")


class StoppingTests(MandateTestCase):
    def setUp(self):
        super().setUp()
        self.family = self.make_family("Bello")
        self.parent = self.make_parent(self.family)

    def test_cancelling_stops_it_at_the_provider_withdraws_consent_and_removes_the_account_details(self):
        mandate = self.active_mandate(self.family)
        cancelled = mandate_provider.cancel(self.manager, mandate.id, reason="The payer changed banks")
        self.assertEqual((cancelled.status, cancelled.is_primary, sealed_of(cancelled)), (MandateStatus.CANCELLED, False, b""))
        self.assertEqual(self.provider_row(mandate).status, "cancelled")
        self.assertIsNotNone(MandateConsent.objects.get(mandate=mandate).withdrawn_at)
        self.assertFalse(mandates.is_debit_ready(cancelled)[0])
        self.assertEqual(mandate_provider.cancel(self.manager, mandate.id).status, MandateStatus.CANCELLED)  # again: nothing more happens

    def test_a_cancelled_mandate_is_never_revived_by_the_provider_still_saying_active(self):
        mandate = self.active_mandate(self.family)
        mandate_provider.cancel(self.manager, mandate.id)
        SandboxMandate.objects.filter(provider_ref=mandate.provider_mandate_reference).update(status="active")
        self.assertEqual(mandate_provider.refresh_mandate(mandate.id).status, MandateStatus.CANCELLED)

    def test_the_payer_can_always_cancel_their_own_mandate(self):
        mandate = self.active_mandate(self.family)
        cancelled = mandate_provider.payer_cancel(self.parent, mandate.id)
        self.assertEqual(cancelled.status, MandateStatus.CANCELLED)
        other = self.make_parent(self.make_family("Sani"))
        with self.assertRaises(NotFound):
            mandate_provider.payer_cancel(other, self.active_mandate(self.make_family("Musa"), account="0987654321").id)

    def test_only_a_manager_can_cancel_suspend_or_reactivate(self):
        mandate = self.active_mandate(self.family)
        for who in (self.maker, self.checker):
            for action in (mandate_provider.cancel, mandate_provider.suspend, mandate_provider.reactivate):
                with self.assertRaises(MandateRefused):
                    action(who, mandate.id)

    def test_a_suspended_mandate_cannot_be_debited_and_can_be_brought_back(self):
        mandate = self.active_mandate(self.family)
        suspended = mandate_provider.suspend(self.manager, mandate.id)
        self.assertEqual(suspended.status, MandateStatus.SUSPENDED)
        self.assertIn("suspended", mandates.is_debit_ready(suspended)[1])
        back = mandate_provider.reactivate(self.manager, mandate.id)
        self.assertEqual(back.status, MandateStatus.ACTIVE)

    def test_a_provider_that_has_no_pause_is_suspended_by_schoolos_and_the_provider_saying_active_does_not_undo_it(self):
        connector = SandboxMandateConnector()
        no_pause = replace(connector.info.capabilities, supports_suspension=False, supports_reactivation=False)
        with mock.patch.object(type(connector), "info", replace(connector.info, capabilities=no_pause)):
            mandate = self.active_mandate(self.family)
            suspended = mandate_provider.suspend(self.manager, mandate.id)
            self.assertEqual(suspended.status, MandateStatus.SUSPENDED)
            self.assertEqual(self.provider_row(mandate).status, "active")  # the provider was not asked to change it
            self.assertEqual(mandate_provider.refresh_mandate(mandate.id).status, MandateStatus.SUSPENDED)

    def test_the_provider_can_report_expiry_and_failure(self):
        mandate = self.active_mandate(self.family)
        SandboxMandate.objects.filter(provider_ref=mandate.provider_mandate_reference).update(status="expired")
        self.assertEqual(mandate_provider.refresh_mandate(mandate.id).status, MandateStatus.EXPIRED)
        self.assertFalse(mandates.is_debit_ready(self.reload(mandate))[0])

    def test_when_the_provider_refuses_the_setup_the_mandate_fails_and_the_details_are_removed(self):
        with mock.patch.object(SandboxMandateConnector, "create_mandate", side_effect=__import__("apps.mandates.providers.base", fromlist=["ProviderRejected"]).ProviderRejected("provider_rejected", "no")):
            mandate = self.start_mandate(self.family)
        self.assertEqual((mandate.status, mandate.failure_code, sealed_of(mandate)), (MandateStatus.FAILED, "provider_rejected", b""))

    def test_when_the_provider_does_not_answer_the_setup_can_be_retried_and_makes_one_mandate(self):
        sandbox.inject_fault("create_mandate", "unavailable")
        mandate = self.start_mandate(self.family)
        self.assertEqual((mandate.status, mandate.failure_code), (MandateStatus.DRAFT, "provider_unavailable"))
        retried = mandate_provider.retry_setup(self.manager, mandate.id)
        self.assertEqual(retried.status, MandateStatus.PENDING_ACTIVATION)
        again = mandate_provider.send_to_provider(mandate.id)  # asking again changes nothing
        self.assertEqual((again.provider_mandate_reference, SandboxMandate.objects.count()), (retried.provider_mandate_reference, 1))

    def test_a_timeout_after_the_provider_made_it_still_ends_with_one_mandate(self):
        sandbox.inject_fault("create_mandate", "timeout_after")
        mandate = self.start_mandate(self.family)
        self.assertEqual(mandate.status, MandateStatus.DRAFT)
        self.assertEqual(SandboxMandate.objects.count(), 1)  # it did happen at the provider
        mandate_provider.retry_setup(self.manager, mandate.id)
        self.assertEqual(SandboxMandate.objects.count(), 1)  # and the retry found it (same reference), it did not make a second

    def test_account_details_are_kept_only_for_a_provider_that_needs_them_again_to_debit(self):
        connector = SandboxMandateConnector()
        needs = replace(connector.info.capabilities, requires_account_number_for_debit=True)
        with mock.patch.object(type(connector), "info", replace(connector.info, capabilities=needs)):
            mandate = self.active_mandate(self.family)
            self.assertTrue(sealed_of(mandate))  # kept, sealed, for the debit
            mandate_provider.cancel(self.manager, mandate.id)
        self.assertEqual(sealed_of(mandate), b"")  # and gone once the mandate ends
        other = self.active_mandate(self.make_family("Sani"), account="0987654321")
        self.assertEqual(sealed_of(other), b"")  # this provider does not need it: removed as soon as it was activated

    def test_the_manage_and_provider_duties_are_different(self):
        self.give_duties(self.manager, PROVIDER)
        with self.assertRaises(MandateRefused):
            self.start_mandate(self.family, who=self.manager)  # provider authority is not mandate authority
        self.give_duties(self.manager, MANAGE)
        self.assertTrue(self.start_mandate(self.family, who=self.manager).id)
