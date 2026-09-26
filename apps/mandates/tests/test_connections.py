"""The school's own Remita and Lendsqr connections: both can exist at once, there is no "active" mandate provider, neither is a Smart Money Collection
provider, credentials are sealed and never returned, and only someone the owner authorised can manage them."""

import json

from django.db import IntegrityError, transaction
from rest_framework.exceptions import NotFound, PermissionDenied

from apps.bankconnect.providers import registry as collection_registry
from apps.bankconnect.providers.transport import HttpResult, use_transport
from apps.bankconnect.tests.fake_transport import FakeTransport, ok

from .. import connections, mandate_provider, serializers
from ..constants import MANDATE_PROVIDERS, ConnectionStatus
from ..errors import MandateRefused
from ..models import MandateAuditEvent, MandateProviderConnection
from ..providers import registry
from ..vault import VaultError, context_for, get_vault
from .base import PROVIDER, MandateTestCase
from .test_lendsqr import BANK_ROWS, BANKS
from .test_remita import BASE as REMITA_BASE
from .test_remita import CREDS as REMITA_CREDS

LENDSQR_KEY = "LENDSQR-KEY-UNIQUE-SECRET"


def both_providers() -> FakeTransport:
    return FakeTransport().on("POST", REMITA_BASE + "/status", ok({"statuscode": "074", "status": "No Available Record"})).on("GET", BANKS, ok(BANK_ROWS))


class RegistryTests(MandateTestCase):
    def test_only_remita_and_lendsqr_are_mandate_providers_plus_the_sandbox_in_development(self):
        self.assertEqual([p.code for p in registry.all_providers()], ["remita", "lendsqr", "sandbox"])
        self.assertEqual(MANDATE_PROVIDERS, ("remita", "lendsqr"))
        for gone in ("paystack", "monnify", "gtbank", "nope", ""):
            self.assertIsNone(registry.get_connector(gone), gone)

    def test_neither_is_a_smart_money_collection_provider(self):
        for code in ("remita", "lendsqr"):
            self.assertIsNone(collection_registry.get_connector(code), code)
        self.assertEqual([p.code for p in collection_registry.all_providers() if not p.is_sandbox], ["paystack", "monnify"])

    def test_the_smart_money_collection_connect_call_refuses_both(self):
        from apps.bankconnect import provider_connections as collection

        for code in ("remita", "lendsqr"):
            with self.assertRaises(collection.CollectionRejected) as caught:
                collection.connect(self.owner, provider=code, environment="test", credentials={"secret_key": "x"})
            self.assertEqual(caught.exception.code, "unknown_provider")

    def test_a_smart_money_collection_provider_cannot_be_connected_for_mandates(self):
        for code in ("paystack", "monnify"):
            with self.assertRaises(MandateRefused) as caught:
                connections.connect(self.owner, provider=code, environment="test", credentials={"secret_key": "x"})
            self.assertEqual(caught.exception.code, "unknown_provider")

    def test_the_database_refuses_a_connection_to_anything_but_a_mandate_provider(self):
        for code in ("paystack", "monnify", "gtbank"):
            with self.assertRaises(IntegrityError), transaction.atomic():
                MandateProviderConnection.objects.create(school=self.school, provider=code, status=ConnectionStatus.CONNECTED)

    def test_the_sandbox_is_absent_where_it_is_switched_off(self):
        from django.test import override_settings

        with override_settings(MANDATES_ENABLE_SANDBOX=False):
            self.assertEqual([p.code for p in registry.all_providers()], ["remita", "lendsqr"])
            self.assertIsNone(registry.get_connector("sandbox"))


class ConnectingBothTests(MandateTestCase):
    def setUp(self):
        super().setUp()
        self.server = both_providers()
        context = use_transport(self.server)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def connect_remita(self, who=None, **kw):
        return connections.connect(who or self.owner, provider="remita", environment="test", label="Remita fees", credentials=dict(REMITA_CREDS), **kw)

    def connect_lendsqr(self, who=None):
        return connections.connect(who or self.owner, provider="lendsqr", environment="test", label="Lendsqr fees", credentials={"api_key": LENDSQR_KEY})

    def test_a_school_connects_both_at_once_and_neither_is_active(self):
        remita, lendsqr = self.connect_remita(), self.connect_lendsqr()
        self.assertEqual((remita.status, lendsqr.status), (ConnectionStatus.CONNECTED, ConnectionStatus.CONNECTED))
        self.assertEqual({c.provider for c in connections.list_connections(self.owner)}, {"sandbox", "remita", "lendsqr"})
        fields = {f.name for f in MandateProviderConnection._meta.get_fields()}
        self.assertFalse({"is_active_provider", "is_active"} & fields)  # there is no active mandate provider at all
        self.assertEqual(self.reload(remita).status, ConnectionStatus.CONNECTED)  # connecting the second changed nothing about the first

    def test_connecting_a_provider_twice_is_refused_and_replacing_credentials_is_the_way(self):
        self.connect_remita()
        with self.assertRaises(MandateRefused) as caught:
            self.connect_remita()
        self.assertEqual(caught.exception.code, "already_connected")

    def test_the_database_allows_one_live_connection_per_provider_per_school_and_not_more(self):
        MandateProviderConnection.objects.create(school=self.school, provider="remita", status=ConnectionStatus.CONNECTED)
        with self.assertRaises(IntegrityError), transaction.atomic():
            MandateProviderConnection.objects.create(school=self.school, provider="remita", status=ConnectionStatus.CONNECTED)
        MandateProviderConnection.objects.create(school=self.other_school, provider="remita", status=ConnectionStatus.CONNECTED)
        MandateProviderConnection.objects.create(school=self.school, provider="lendsqr", status=ConnectionStatus.CONNECTED)

    def test_credentials_are_sealed_bound_to_their_connection_and_never_returned(self):
        remita, lendsqr = self.connect_remita(), self.connect_lendsqr()
        for connection, secrets in ((remita, (REMITA_CREDS["api_key"], REMITA_CREDS["api_token"])), (lendsqr, (LENDSQR_KEY,))):
            sealed = bytes(self.reload(connection).sealed_credentials)
            for secret in secrets:
                self.assertNotIn(secret.encode(), sealed)
            opened = get_vault().open(context_for(connection), sealed)
            self.assertIn(secrets[0], opened.values())
            with self.assertRaises(VaultError):  # a blob copied to another connection's row will not open
                get_vault().open(context_for(lendsqr if connection.id == remita.id else remita), sealed)
            shown = json.dumps(serializers.connection(connection))
            for secret in secrets:
                self.assertNotIn(secret, shown)
        blob = json.dumps([e.detail for e in MandateAuditEvent.objects.all()])
        for secret in (REMITA_CREDS["api_key"], REMITA_CREDS["api_token"], LENDSQR_KEY):
            self.assertNotIn(secret, blob)

    def test_a_blob_from_a_collection_connection_will_not_open_as_a_mandate_credential(self):
        remita = self.connect_remita()
        from apps.bankconnect.vault import context_for as collection_context

        sealed = get_vault().seal(collection_context(remita), {"secret_key": "x"})  # sealed the way Smart Money Collection seals
        with self.assertRaises(VaultError):
            get_vault().open(context_for(remita), sealed)

    def test_only_someone_the_owner_authorised_can_manage_providers(self):
        for who in (self.maker, self.checker, self.manager, self.members["teacher"], self.members["parent"]):
            with self.assertRaises(PermissionDenied):
                self.connect_remita(who=who)
        self.give_duties(self.members["accountant"], "finance.collection_provider_manage", "finance.billing_authority", "finance.bank_connections")
        with self.assertRaises(PermissionDenied):  # the Smart Money Collection duty is not this one
            self.connect_remita(who=self.members["accountant"])
        self.give_duties(self.members["accountant"], PROVIDER)
        self.assertEqual(self.connect_remita(who=self.members["accountant"]).created_by_id, self.members["accountant"].id)

    def test_one_schools_connection_is_invisible_to_another(self):
        remita = self.connect_remita()
        with self.assertRaises(NotFound):
            connections.get_connection(self.other_owner, remita.id)
        with self.assertRaises(NotFound):
            connections.test(self.other_owner, remita.id)

    def test_new_credentials_for_a_different_merchant_are_refused(self):
        remita = self.connect_remita()
        with self.assertRaises(MandateRefused) as caught:
            connections.replace_credentials(self.owner, remita.id, credentials={**REMITA_CREDS, "merchant_id": "9999999"})
        self.assertEqual(caught.exception.code, "different_merchant")
        same = connections.replace_credentials(self.owner, remita.id, credentials={**REMITA_CREDS, "api_key": "A-NEW-KEY"})
        self.assertEqual(same.status, ConnectionStatus.CONNECTED)

    def test_a_refused_credential_marks_the_connection_as_needing_new_ones(self):
        remita = self.connect_remita()
        self.server.on("POST", REMITA_BASE + "/status", ok({"statuscode": "020"}))
        _, result = connections.test(self.owner, remita.id)
        self.assertEqual((result.ok, result.code, self.reload(remita).status), (False, "bad_credentials", ConnectionStatus.NEEDS_REAUTH))
        self.server.on("POST", REMITA_BASE + "/status", ok({"statuscode": "074"}))
        self.assertTrue(connections.test(self.owner, remita.id)[1].ok)
        self.assertEqual(self.reload(remita).status, ConnectionStatus.CONNECTED)

    def test_a_provider_that_does_not_answer_is_an_error_not_a_refusal(self):
        remita = self.connect_remita()
        self.server.on("POST", REMITA_BASE + "/status", HttpResult(503, None, ""))
        _, result = connections.test(self.owner, remita.id)
        self.assertEqual((result.code, self.reload(remita).status), ("provider_unavailable", ConnectionStatus.ERROR))

    def test_families_can_hold_mandates_with_different_providers_at_the_same_time(self):
        lendsqr = self.connect_lendsqr()
        self.server.on("GET", "/v2/customers/1000/direct-debit-mandates", ok({"status": "success", "data": {"mandates": []}}))
        self.server.on("POST", "/v2/customers/direct-debit-mandates", ok({"status": "success", "data": {
            "id": 16391, "status": "pending_mandate_activation", "mandate_id": "RC1/2/3", "registration_date": "2026-09-26T10:00:00.000Z",
            "start_date": "2026-09-26T00:00:00.000Z", "end_date": "2028-09-26T00:00:00.000Z"}}))
        first, second = self.make_family("Bello"), self.make_family("Sani")
        with_sandbox = self.start_mandate(first)
        with_lendsqr = self.start_mandate(second, connection=lendsqr, bank="044", provider_customer_ref="1000", account="0987654321")
        self.assertEqual((with_sandbox.provider, with_lendsqr.provider), ("sandbox", "lendsqr"))
        self.assertEqual((with_sandbox.provider_connection_id, with_lendsqr.provider_connection_id), (self.connection.id, lendsqr.id))
        counts = {c.provider: serializers.connection(c, counts=None)["provider"] for c in connections.list_connections(self.owner)}
        self.assertEqual(set(counts), {"sandbox", "lendsqr"})

    def test_a_connection_with_live_mandates_cannot_be_disabled_or_disconnected_but_one_without_can(self):
        family = self.make_family("Bello")
        mandate = self.start_mandate(family)
        for action in (connections.disable, connections.disconnect):
            with self.assertRaises(MandateRefused) as caught:
                action(self.owner, self.connection.id)
            self.assertEqual(caught.exception.code, "has_live_mandates")
        mandate_provider.cancel(self.manager, mandate.id)
        disabled = connections.disable(self.owner, self.connection.id)
        self.assertEqual(disabled.status, ConnectionStatus.DISABLED)
        again, result = connections.enable(self.owner, self.connection.id)
        self.assertTrue(result.ok)
        gone = connections.disconnect(self.owner, self.connection.id)
        self.assertEqual((gone.status, bytes(gone.sealed_credentials)), (ConnectionStatus.REVOKED, b""))
        self.assertIsNotNone(again)

    def test_lendsqr_live_is_refused_until_switched_on_and_remita_live_until_its_address_is_set(self):
        for provider, creds in (("lendsqr", {"api_key": LENDSQR_KEY}), ("remita", dict(REMITA_CREDS))):
            with self.assertRaises(MandateRefused) as caught:
                connections.connect(self.owner, provider=provider, environment="live", credentials=creds)
            self.assertIn(caught.exception.code, ("live_not_enabled", "live_not_configured"))
        self.assertFalse(MandateProviderConnection.objects.filter(provider__in=["remita", "lendsqr"]).exists())

    def test_the_callback_address_is_shown_only_to_a_provider_manager_and_renewed_on_request(self):
        remita = self.connect_remita()
        setup = connections.webhook_setup(self.owner, remita.id)
        self.assertRegex(setup["path"], r"^mandate-webhooks/remita/[A-Za-z0-9_\-]{20,}/$")
        self.assertEqual((setup["verification"], setup["status"]), ("requery", "awaiting_event"))
        before = self.reload(remita).webhook_token_hash
        connections.issue_webhook_token(self.owner, remita.id)
        self.assertNotEqual(self.reload(remita).webhook_token_hash, before)
        with self.assertRaises(PermissionDenied):
            connections.webhook_setup(self.maker, remita.id)
        lendsqr = self.connect_lendsqr()
        with self.assertRaises(MandateRefused) as caught:  # Lendsqr documents no callbacks
            connections.webhook_setup(self.owner, lendsqr.id)
        self.assertEqual(caught.exception.code, "no_webhooks")
