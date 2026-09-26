"""Smart Money Collection works with exactly two providers, Paystack and Monnify. Remita is reserved for a separate Mandates / Direct Debit
feature and is refused by the SERVER wherever a collection provider is chosen: connecting it, making it the active provider, preparing a
batch for it, switching to or from it, and giving a family payment details from it. The interface hiding it is not what these rely on."""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.bankconnect import provider_connections
from apps.bankconnect.provider_connections import CollectionRejected
from apps.bankconnect.models import CollectionProviderConnection
from apps.bankconnect.providers import registry
from apps.bankconnect.providers.monnify import clear_token_cache
from apps.bankconnect.providers.transport import use_transport
from apps.bankconnect.tests.fake_transport import FakeTransport, ok
from apps.bankconnect.tests.test_monnify import CREDS as MONNIFY_CREDS
from apps.bankconnect.tests.test_monnify import login
from apps.receivables import collection_accounts
from apps.receivables.errors import Refused
from apps.receivables.models import FamilyCollectionAccount

from .. import batches, switching
from ..constants import BatchStatus, SwitchStatus
from ..errors import CollectionRefused
from ..models import CollectionGenerationBatch, ProviderSwitch
from .base import CollectTestCase

PAYSTACK_KEY = "sk_test_PROVIDERS-ONLY-KEY"


class ProvidersOnlyCase(CollectTestCase):
    def setUp(self):
        super().setUp()
        clear_token_cache()
        self.addCleanup(clear_token_cache)
        self.server = (
            FakeTransport()
            .on("GET", "/dedicated_account/available_providers", ok({"status": True, "data": []}))
            .on("POST", "/api/v1/auth/login", login())
        )
        context = use_transport(self.server)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def stand_down(self):
        """The school has no active provider (the base case made the sandbox active)."""
        CollectionProviderConnection.objects.update(is_active_provider=False)

    def paystack(self, **kw):
        return self.connect_provider(provider="paystack", credentials={"secret_key": PAYSTACK_KEY}, **kw)

    def monnify(self, **kw):
        return self.connect_provider(provider="monnify", credentials=MONNIFY_CREDS, **kw)

    def old_remita_row(self, **kw):
        """What the earlier design could have left. Made directly: nothing can connect Remita any more."""
        values = {"school": self.school, "provider": "remita", "status": "connected", "environment": "test"}
        values.update(kw)
        return CollectionProviderConnection.objects.create(**values)


class TheProvidersThemselves(ProvidersOnlyCase):
    def test_paystack_and_monnify_are_offered_and_remita_is_not(self):
        offered = [p.code for p in registry.all_providers() if not p.is_sandbox]
        self.assertEqual(offered, ["paystack", "monnify"])
        self.assertIsNone(registry.get_connector("remita"))

    def test_remita_cannot_be_connected_whatever_it_is_given_and_nothing_is_stored(self):
        before = CollectionProviderConnection.objects.count()
        for credentials in ({"merchant_id": "2547916", "api_key": "K", "service_type_id": "4430731"}, {}, {"secret_key": "x"}):
            with self.assertRaises(CollectionRejected) as caught:
                provider_connections.connect(self.owner, provider="remita", environment="test", label="Fees", credentials=credentials)
            self.assertEqual(caught.exception.code, "unknown_provider")
        self.assertEqual(CollectionProviderConnection.objects.count(), before)

    def test_paystack_and_monnify_can_each_be_connected_and_each_be_the_active_provider(self):
        self.stand_down()
        paystack = self.paystack()
        self.assertEqual([c.provider for c in CollectionProviderConnection.objects.filter(is_active_provider=True)], ["paystack"])
        self.stand_down()
        monnify = self.monnify()
        self.assertEqual([c.provider for c in CollectionProviderConnection.objects.filter(is_active_provider=True)], ["monnify"])
        self.assertEqual((paystack.status, monnify.status), ("connected", "connected"))

    def test_a_school_can_have_both_connected_with_exactly_one_active(self):
        self.stand_down()
        paystack, monnify = self.paystack(activate=False), self.monnify(activate=False)
        provider_connections.activate(self.owner, monnify.id)
        with self.assertRaises(CollectionRejected) as caught:
            provider_connections.activate(self.owner, paystack.id)  # a second one is a switch, not an activation
        self.assertEqual(caught.exception.code, "use_switch")
        self.assertEqual([c.provider for c in CollectionProviderConnection.objects.filter(school=self.school, is_active_provider=True)], ["monnify"])


class RemitaCannotBeTheActiveProvider(ProvidersOnlyCase):
    def test_the_activate_call_refuses_it(self):
        self.stand_down()
        remita = self.old_remita_row()
        with self.assertRaises(CollectionRejected) as caught:
            provider_connections.activate(self.owner, remita.id)
        self.assertEqual(caught.exception.code, "unknown_provider")
        remita.refresh_from_db()
        self.assertFalse(remita.is_active_provider)

    def test_the_database_refuses_it_even_if_the_application_did_not(self):
        remita = self.old_remita_row()
        self.stand_down()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CollectionProviderConnection.objects.filter(pk=remita.pk).update(is_active_provider=True)

    def test_a_remita_row_does_not_stop_a_school_connecting_paystack_or_monnify_or_counts_as_a_connected_provider(self):
        self.old_remita_row()
        self.stand_down()
        self.paystack(activate=False)
        self.monnify()
        listed = [c.provider for c in provider_connections.list_connections(self.owner) if c.provider in ("paystack", "monnify")]
        self.assertEqual(sorted(listed), ["monnify", "paystack"])


class RemitaCannotBeBatchedOrSwitchedTo(ProvidersOnlyCase):
    def test_a_batch_is_prepared_for_paystack_or_monnify_and_records_which(self):
        for maker in (self.paystack, self.monnify):
            self.stand_down()
            connection = maker(activate=True)
            self.make_family(f"Family {connection.provider}")
            batch = self.new_batch()
            self.assertEqual((batch.provider, batch.provider_connection_id), (connection.provider, connection.id))
            CollectionGenerationBatch.objects.filter(pk=batch.pk).update(status=BatchStatus.CANCELLED)

    def test_a_batch_cannot_be_prepared_for_a_remita_connection_whatever_asks(self):
        remita = self.old_remita_row()
        self.stand_down()
        with self.assertRaises(CollectionRefused) as caught:
            self.new_batch()
        self.assertEqual(caught.exception.code, "no_active_provider")  # there is nothing to choose: Remita can never be the active provider
        session = self.session
        with self.assertRaises(ValidationError):
            CollectionGenerationBatch.objects.create(
                school=self.school, session=session, provider_connection=remita, provider="remita", environment="test", prepared_by=self.maker,
            )
        self.assertEqual(CollectionGenerationBatch.objects.count(), 0)

    def test_a_provider_switch_between_paystack_and_monnify_works_in_both_directions(self):
        self.stand_down()
        paystack, monnify = self.paystack(), self.monnify(activate=False)
        for target, expected in ((monnify, "monnify"), (paystack, "paystack")):
            switch = switching.schedule(self.owner, to_connection_id=target.id, scheduled_for=timezone.now() - timedelta(minutes=1))
            self.assertEqual(switch.status, SwitchStatus.READY_TO_SWITCH)
            self.assertEqual([c.provider for c in CollectionProviderConnection.objects.filter(is_active_provider=True)], ["paystack" if expected == "monnify" else "monnify"])
            applied = switching.apply(self.owner, switch.id)  # a person applies it; nothing did it by itself
            self.assertEqual(applied.status, SwitchStatus.APPLIED)
            self.assertEqual([c.provider for c in CollectionProviderConnection.objects.filter(is_active_provider=True)], [expected])

    def test_a_switch_to_remita_is_refused(self):
        remita = self.old_remita_row()
        with self.assertRaises(CollectionRefused) as caught:
            switching.schedule(self.owner, to_connection_id=remita.id, scheduled_for=timezone.now() + timedelta(days=1))
        self.assertEqual(caught.exception.code, "provider_unavailable")
        self.assertFalse(ProviderSwitch.objects.exists())

    def test_a_switch_from_remita_is_refused_because_it_can_never_be_the_active_provider(self):
        self.old_remita_row()
        self.stand_down()
        paystack = self.paystack(activate=False)
        with self.assertRaises(CollectionRefused) as caught:
            switching.schedule(self.owner, to_connection_id=paystack.id, scheduled_for=timezone.now() + timedelta(days=1))
        self.assertEqual(caught.exception.code, "no_active_provider")

    def test_a_switch_that_somehow_involves_remita_stays_blocked_and_is_never_applied(self):
        remita = self.old_remita_row()
        switch = ProviderSwitch.objects.create(
            school=self.school, from_connection=self.connection, to_connection=remita, status=SwitchStatus.READY_TO_SWITCH, scheduled_for=timezone.now(),
        )
        with self.assertRaises(CollectionRefused) as caught:
            switching.apply(self.owner, switch.id)
        self.assertEqual(caught.exception.code, "switch_blocked")
        self.assertIn("Paystack and Monnify", caught.exception.args[0] if caught.exception.args else str(caught.exception))
        self.assertEqual([c.pk for c in CollectionProviderConnection.objects.filter(is_active_provider=True)], [self.connection.pk])
        switch.refresh_from_db()
        self.assertEqual(switch.status, SwitchStatus.SCHEDULED)


class NoFamilyPaymentDetailsFromRemita(ProvidersOnlyCase):
    def test_a_family_account_cannot_be_made_from_a_remita_connection(self):
        remita = self.old_remita_row()
        family = self.make_family("Bello")
        with self.assertRaises(ValidationError):
            FamilyCollectionAccount.objects.create(
                school=self.school, family=family, provider="remita", connection=remita, origin="provider", account_number="140008260136",
            )
        self.assertFalse(FamilyCollectionAccount.objects.filter(provider="remita").exists())

    def test_a_family_account_cannot_be_recorded_by_hand_as_a_remita_reference_either(self):
        family = self.make_family("Bello")
        for spelling in ("remita", "Remita", " REMITA "):
            with self.assertRaises(Refused) as caught:
                collection_accounts.register(family, provider=spelling, account_number="140008260136", actor=self.owner)
            self.assertEqual(caught.exception.code, "provider_not_supported")
        self.assertFalse(FamilyCollectionAccount.objects.exists())

    def test_the_provider_information_offers_no_remita_wording(self):
        from apps.bankconnect.serializers import serialize_provider

        for info in registry.all_providers():
            shown = str(serialize_provider(info)).lower()
            self.assertNotIn("remita", shown)
            self.assertNotIn("rrr", shown.replace("mirror", ""))
