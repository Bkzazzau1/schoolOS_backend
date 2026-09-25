import json

from cryptography.fernet import Fernet
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, override_settings

from .. import audit, permissions, vault
from ..constants import ConnectionStatus, ConnectionType, Direction
from ..models import BankConnection, BankTransaction
from ..providers import registry
from ..providers.base import BadCredentials, ConnectorError, InvalidSignature, NotSupported
from ..providers.sandbox import SIGNATURE_HEADER, SandboxConnector
from .base import BankTestCase

CONTEXT = {"school": "s1", "connection": "c1"}


class VaultTests(SimpleTestCase):
    def test_a_secret_round_trips_and_the_stored_bytes_do_not_contain_it(self):
        v = vault.FernetVault([Fernet.generate_key().decode()])
        blob = v.seal(CONTEXT, {"api_key": "super-secret-value"})
        self.assertNotIn(b"super-secret-value", blob)
        self.assertEqual(v.open(CONTEXT, blob), {"api_key": "super-secret-value"})

    def test_a_blob_copied_to_another_school_or_connection_does_not_open(self):
        v = vault.FernetVault([Fernet.generate_key().decode()])
        blob = v.seal(CONTEXT, {"api_key": "x"})
        for other in ({"school": "s2", "connection": "c1"}, {"school": "s1", "connection": "c2"}):
            with self.assertRaises(vault.VaultError):
                v.open(other, blob)

    def test_a_tampered_or_foreign_blob_does_not_open_and_the_error_holds_no_secret(self):
        v = vault.FernetVault([Fernet.generate_key().decode()])
        blob = v.seal(CONTEXT, {"api_key": "hunter2-secret"})
        with self.assertRaises(vault.VaultError) as raised:
            v.open(CONTEXT, blob[:-4] + b"AAAA")
        self.assertNotIn("hunter2", str(raised.exception))
        with self.assertRaises(vault.VaultError):
            vault.FernetVault([Fernet.generate_key().decode()]).open(CONTEXT, blob)

    def test_a_key_is_rotated_by_adding_a_new_one_in_front(self):
        old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        blob = vault.FernetVault([old]).seal(CONTEXT, {"api_key": "x"})
        both = vault.FernetVault([new, old])
        self.assertEqual(both.open(CONTEXT, blob), {"api_key": "x"})
        resealed = both.reseal(blob)
        self.assertEqual(vault.FernetVault([new]).open(CONTEXT, resealed), {"api_key": "x"})
        with self.assertRaises(vault.VaultError):
            vault.FernetVault([new]).open(CONTEXT, blob)

    @override_settings(BANKCONNECT_SECRET_KEYS=[])
    def test_with_no_key_nothing_can_be_stored(self):
        with self.assertRaises(vault.VaultNotConfigured):
            vault.get_vault()

    @override_settings(BANKCONNECT_SECRET_KEYS=["not-a-key"])
    def test_an_invalid_key_fails_closed(self):
        with self.assertRaises(vault.VaultNotConfigured):
            vault.get_vault()


class RegistryTests(SimpleTestCase):
    @override_settings(BANKCONNECT_ENABLE_SANDBOX=False)
    def test_the_real_banks_are_listed_but_have_no_capabilities_and_cannot_connect(self):
        real = registry.all_providers()
        self.assertEqual(
            {p.code for p in real},
            {"gtbank", "uba", "zenith", "access", "firstbank", "moniepoint", "opay", "open_banking", "monnify", "paystack"},
        )
        for info in real:
            self.assertEqual(info.production_status, "pending_verified_documentation", info.code)
            self.assertFalse(any(info.capabilities.as_dict().values()), info.code)
            self.assertFalse(info.implemented)
            with self.assertRaises(ConnectorError) as raised:
                registry.get_connector(info.code).connect(credentials={"anything": "x"})
            self.assertEqual(raised.exception.code, "pending_documentation")

    @override_settings(BANKCONNECT_ENABLE_SANDBOX=False)
    def test_a_collection_provider_is_never_the_same_kind_as_a_bank_account(self):
        kinds = {p.code: p.connection_type for p in registry.all_providers()}
        self.assertEqual(kinds["paystack"], ConnectionType.COLLECTION_PROVIDER)
        self.assertEqual(kinds["monnify"], ConnectionType.COLLECTION_PROVIDER)
        self.assertEqual(kinds["gtbank"], ConnectionType.DIRECT_BANK_API)
        self.assertEqual(kinds["open_banking"], ConnectionType.OPEN_BANKING)

    @override_settings(BANKCONNECT_ENABLE_SANDBOX=False)
    def test_the_sandbox_is_absent_unless_switched_on(self):
        self.assertNotIn("sandbox", [p.code for p in registry.all_providers()])
        self.assertIsNone(registry.get_connector("sandbox"))

    @override_settings(BANKCONNECT_ENABLE_SANDBOX=True)
    def test_the_sandbox_appears_when_switched_on(self):
        self.assertIn("sandbox", [p.code for p in registry.all_providers()])

    @override_settings(BANKCONNECT_ENABLE_SANDBOX=False)
    def test_a_connector_only_does_what_it_declares(self):
        pending = registry.get_connector("gtbank")
        with self.assertRaises(NotSupported):
            pending.fetch_transactions({}, {})
        with self.assertRaises(NotSupported):
            pending.handle_webhook(raw_body=b"", headers={}, secret={})


class SandboxConnectorTests(SimpleTestCase):
    def setUp(self):
        self.connector = SandboxConnector()

    def test_connecting_with_a_key_verifies_the_account_identity(self):
        grant = self.connector.connect(credentials={"sandbox_key": "sandbox-abc", "account_number": "0123456789"})
        self.assertEqual((grant.identity.bank_name, grant.identity.account_number), ("Sandbox Bank", "0123456789"))
        self.assertIsNone(grant.token_expires_at)
        self.assertIn("webhook_secret", grant.secret)

    def test_bad_credentials_are_refused(self):
        for credentials in ({}, {"sandbox_key": "nope", "account_number": "0123456789"}, {"sandbox_key": "sandbox-x", "account_number": "12"}):
            with self.assertRaises(BadCredentials):
                self.connector.connect(credentials=credentials)

    def test_the_authorisation_flow_redirects_to_the_provider_never_asks_for_a_password(self):
        start = self.connector.begin_authorization(redirect_uri="https://app.example/cb", state="abc")
        self.assertIn("state=abc", start.authorization_url)
        grant = self.connector.connect(authorization_code="sandbox-approved")
        self.assertIsNotNone(grant.token_expires_at)
        with self.assertRaises(BadCredentials):
            self.connector.connect(authorization_code="wrong")

    def test_the_access_token_can_be_refreshed(self):
        grant = self.connector.connect(authorization_code="sandbox-approved")
        renewed, expires = self.connector.refresh_access_token(grant.secret)
        self.assertNotEqual(renewed["access_token"], grant.secret["access_token"])
        self.assertIsNotNone(expires)

    def test_a_webhook_needs_a_valid_signature(self):
        secret = {"webhook_secret": "shh", "sandbox_key": "sandbox-x"}
        body = json.dumps({"transaction": {"external_transaction_id": "T1", "amount_minor": 5000}}).encode()
        good = self.connector.sign(body, "shh")
        [tx] = self.connector.handle_webhook(raw_body=body, headers={SIGNATURE_HEADER: good}, secret=secret)
        self.assertEqual((tx.external_transaction_id, tx.amount_minor), ("T1", 5000))
        for headers in ({}, {SIGNATURE_HEADER: "0" * 64}):
            with self.assertRaises(InvalidSignature):
                self.connector.handle_webhook(raw_body=body, headers=headers, secret=secret)


class PermissionTests(BankTestCase):
    def test_the_owner_manages_and_views(self):
        self.assertTrue(permissions.can_manage_connections(self.members["proprietor"]))
        self.assertTrue(permissions.can_view_collections(self.members["proprietor"]))

    def test_the_finance_office_views_but_cannot_manage_without_the_duty(self):
        finance = self.members["accountant"]
        self.assertTrue(permissions.can_view_collections(finance))
        self.assertFalse(permissions.can_manage_connections(finance))
        self.give_duty(finance)
        self.assertTrue(permissions.can_manage_connections(finance))

    def test_nobody_else_gets_anything_by_role_alone(self):
        for role in ("principal", "administrator", "teacher", "staff", "parent", "student", "driver"):
            self.assertFalse(permissions.can_view_collections(self.members[role]), role)
            self.assertFalse(permissions.can_manage_connections(self.members[role]), role)

    def test_a_duty_that_is_not_active_grants_nothing(self):
        finance = self.members["accountant"]
        self.give_duty(finance, status="revoked")
        self.assertFalse(permissions.can_manage_connections(finance))


class AuditTests(BankTestCase):
    def test_anything_that_looks_like_a_secret_is_dropped_from_the_trail(self):
        event = audit.record(
            self.school, "connected", actor=self.owner,
            provider="sandbox", api_key="AAA", client_secret="BBB", access_token="CCC", password="DDD",
            nested={"authorization": "EEE", "kept": "yes"}, note="x" * 500,
        )
        blob = json.dumps(event.detail)
        for leaked in ("AAA", "BBB", "CCC", "DDD", "EEE"):
            self.assertNotIn(leaked, blob)
        self.assertEqual(event.detail["provider"], "sandbox")
        self.assertEqual(event.detail["nested"], {"kept": "yes"})
        self.assertEqual(len(event.detail["note"]), 200)


class ModelConstraintTests(BankTestCase):
    def connection(self, **over):
        fields = dict(school=self.school, provider="sandbox", connection_type=ConnectionType.SANDBOX,
                      account_fingerprint="f" * 64, status=ConnectionStatus.CONNECTED)
        fields.update(over)
        return BankConnection.objects.create(**fields)

    def test_the_same_account_cannot_be_connected_twice_to_one_school(self):
        self.connection()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.connection()

    def test_a_disconnected_account_can_be_connected_again_and_another_school_may_have_it(self):
        first = self.connection()
        first.status = ConnectionStatus.REVOKED
        first.save()
        self.connection()
        self.connection(school=self.other_school)

    def transaction(self, connection, **over):
        fields = dict(school=self.school, connection=connection, provider="sandbox", external_transaction_id="T1",
                      direction=Direction.CREDIT, amount_minor=1000, transaction_date="2026-09-25T10:00:00Z")
        fields.update(over)
        return BankTransaction.objects.create(**fields)

    def test_the_same_external_id_is_one_transaction_per_connection(self):
        c = self.connection()
        self.transaction(c)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.transaction(c)

    def test_a_provider_session_is_one_transaction_per_school(self):
        a, b = self.connection(), self.connection(account_fingerprint="e" * 64)
        self.transaction(a, provider_session_id="S1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.transaction(b, external_transaction_id="T2", provider_session_id="S1")
        self.transaction(b, external_transaction_id="T3", provider_session_id="")
        self.transaction(b, external_transaction_id="T4", provider_session_id="")

    def test_an_amount_must_be_positive(self):
        c = self.connection()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.transaction(c, amount_minor=0)
