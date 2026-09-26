"""The payer identity number some providers need: written and never read back, sealed to one family, and used at the moment an account is made.
Also Monnify end to end, the provider that needs it."""

import json

from django.core.exceptions import ValidationError

from apps.bankconnect.models import BankAuditEvent
from apps.bankconnect.providers.monnify import clear_token_cache
from apps.bankconnect.providers.transport import use_transport
from apps.bankconnect.tests.fake_transport import FakeTransport
from apps.bankconnect.tests.test_monnify import CREDS as MONNIFY_CREDS
from apps.bankconnect.tests.test_monnify import RESERVE, login, reserved

from .. import batches, identity, jobs, running
from ..constants import Eligibility, GenerationStatus
from ..errors import CollectionRefused
from ..models import CollectionAuditEvent, FamilyPayerIdentity
from .base import PROVIDER, CollectTestCase

BVN, NIN = "22222222222", "33333333333"


class IdentityTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.family = self.make_family("Alpha")

    def test_it_is_sealed_and_only_the_fact_that_it_is_on_file_is_ever_shown(self):
        identity.save(self.maker, self.family, bvn=BVN)
        row = FamilyPayerIdentity.objects.get(family=self.family)
        self.assertNotIn(BVN.encode(), bytes(row.sealed))
        self.assertEqual(identity.status_of(self.family), {"hasBvn": True, "hasNin": False})
        self.assertEqual(identity.open_for_generation(self.family), {"bvn": BVN})

    def test_a_second_number_is_added_beside_the_first(self):
        identity.save(self.maker, self.family, bvn=BVN)
        identity.save(self.maker, self.family, nin=NIN)
        self.assertEqual(identity.open_for_generation(self.family), {"bvn": BVN, "nin": NIN})
        self.assertEqual(identity.status_of(self.family), {"hasBvn": True, "hasNin": True})

    def test_it_is_bound_to_its_family_so_a_copy_in_another_family_does_not_open(self):
        other = self.make_family("Other")
        identity.save(self.maker, self.family, bvn=BVN)
        identity.save(self.maker, other, nin=NIN)
        mine = FamilyPayerIdentity.objects.get(family=self.family)
        FamilyPayerIdentity.objects.filter(family=other).update(sealed=mine.sealed)
        self.assertEqual(identity.open_for_generation(other), {})

    def test_digits_only_and_exactly_eleven(self):
        for bad in ("123", "abcdefghijk", "2222222222x", "222222222222"):
            with self.assertRaises(CollectionRefused) as caught:
                identity.save(self.maker, self.family, bvn=bad)
            self.assertEqual(caught.exception.code, "invalid_identity")
        with self.assertRaises(CollectionRefused) as caught:
            identity.save(self.maker, self.family)
        self.assertEqual(caught.exception.code, "identity_required")

    def test_only_someone_who_prepares_or_manages_providers_can_record_one_and_only_for_their_own_school(self):
        for who in (self.checker, self.members["teacher"]):
            with self.assertRaises(CollectionRefused):
                identity.save(who, self.family, bvn=BVN)
        self.give_duties(self.checker, PROVIDER)
        identity.save(self.checker, self.family, bvn=BVN)
        with self.assertRaises(CollectionRefused) as caught:
            identity.save(self.other_owner, self.family, bvn=BVN)
        self.assertEqual(caught.exception.code, "family_not_found")

    def test_the_number_appears_in_no_audit_trail(self):
        identity.save(self.maker, self.family, bvn=BVN, nin=NIN)
        blob = str(list(CollectionAuditEvent.objects.values_list("detail", flat=True))) + str(list(BankAuditEvent.objects.values_list("detail", flat=True)))
        self.assertNotIn(BVN, blob)
        self.assertNotIn(NIN, blob)
        self.assertTrue(CollectionAuditEvent.objects.filter(kind="payer_identity_saved", detail__has_bvn=True).exists())

    def test_the_row_is_for_one_family_of_one_school(self):
        other_family = self.make_family("Other", school=self.other_school) if False else None
        self.assertIsNone(other_family)
        with self.assertRaises(ValidationError):
            FamilyPayerIdentity(school=self.other_school, family=self.family).save()

    def test_it_can_be_resealed_with_a_new_key_by_the_same_command_as_the_provider_credentials(self):
        from django.core.management import call_command

        identity.save(self.maker, self.family, bvn=BVN)
        call_command("reseal_bank_credentials")
        self.assertEqual(identity.open_for_generation(self.family), {"bvn": BVN})


class MonnifyEndToEndTests(CollectTestCase):
    """Monnify needs the payer's BVN or NIN: until it is recorded the family is not selectable, and once it is, it goes to Monnify in the documented request."""

    def setUp(self):
        super().setUp()
        clear_token_cache()
        self.addCleanup(clear_token_cache)
        self.server = FakeTransport().on("POST", "/api/v1/auth/login", login()).on(
            "POST", RESERVE, lambda call: reserved(number="6000000042", reference=call.json["accountReference"])
        )
        context = use_transport(self.server)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        # Monnify is the school's active provider instead of the sandbox.
        from apps.bankconnect.models import CollectionProviderConnection

        CollectionProviderConnection.objects.filter(pk=self.connection.pk).update(is_active_provider=False)
        self.monnify = self.connect_provider(provider="monnify", credentials=MONNIFY_CREDS, activate=True)
        self.family = self.make_family("Alpha")

    def test_the_family_is_not_ready_until_the_identity_number_is_recorded(self):
        batch = self.new_batch()
        item = self.item(batch, self.family)
        self.assertEqual((item.eligibility_status, item.selected), (Eligibility.MISSING_DETAILS, False))
        self.assertIn("BVN or NIN", item.eligibility_note)
        with self.assertRaises(CollectionRefused):
            batches.set_selection(self.maker, batch.id, select=[str(item.id)])

    def test_once_recorded_the_account_is_made_with_the_number_sent_to_monnify_and_shown_nowhere(self):
        batch = self.new_batch()
        identity.save(self.maker, self.family, bvn=BVN)
        batches.refresh_preview(self.maker, batch.id)
        self.assertEqual(self.item(batch, self.family).eligibility_status, Eligibility.ELIGIBLE)
        batch = self.refetch(batch)
        batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        batches.approve(self.checker, batch.id, expected_hash=self.refetch(batch).snapshot_hash)
        running.start_processing(self.maker, batch.id)
        self.assertEqual(self.server.called("POST", RESERVE), 0)  # not until the worker runs
        jobs.drain()
        item = self.item(batch, self.family)
        self.assertEqual(item.generation_status, GenerationStatus.SUCCESS)
        (call,) = self.server.to("POST", RESERVE)
        self.assertEqual((call.json["bvn"], call.json["accountReference"], call.json["customerEmail"]), (BVN, item.provider_request_reference, "parent@example.com"))
        (account,) = self.live_accounts(self.family)
        self.assertEqual((account.provider, account.account_number, account.external_account_ref), ("monnify", "6000000042", item.provider_request_reference))
        stored = json.dumps([item.checkpoint, item.policy_snapshot, account.provider_meta, list(CollectionAuditEvent.objects.values_list("detail", flat=True))], default=str)
        self.assertNotIn(BVN, stored)

    def test_an_identity_number_can_be_a_nin_instead(self):
        identity.save(self.maker, self.family, nin=NIN)
        batch = self.new_batch()
        self.assertEqual(self.item(batch, self.family).eligibility_status, Eligibility.ELIGIBLE)
