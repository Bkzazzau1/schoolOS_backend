"""A school's own collection-provider connections: who may manage them, that no credential ever leaves the server, that one school can
never see or touch another's, and the rules for the ONE active provider."""

import json

from cryptography.fernet import Fernet
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, override_settings

from .. import audit, permissions, vault
from ..constants import ConnectionStatus
from ..models import BankAuditEvent, CollectionProviderConnection
from ..providers import registry
from ..providers.base import NotSupported
from .base import SANDBOX_KEY, BankTestCase

PROVIDER_ROLES = ("principal", "administrator", "teacher", "staff", "parent", "student", "driver")
ALL_DUTIES = (
    "finance.collection_provider_manage", "finance.collection_policy_manage", "finance.collection_prepare", "finance.collection_approve",
)


class ProvidersApiTests(BankTestCase):
    def test_the_owner_sees_exactly_the_three_supported_providers_and_the_sandbox_only_because_it_is_on(self):
        body = self.api_get("providers/").json()
        codes = [p["code"] for p in body["providers"]]
        self.assertEqual(codes, ["paystack", "monnify", "remita", "sandbox"])
        for gone in ("gtbank", "uba", "zenith", "access", "firstbank", "opay", "moniepoint", "open_banking"):
            self.assertNotIn(gone, codes)
        self.assertTrue(body["canManage"] and body["secureStorageReady"])
        self.assertIsNone(body["activeConnectionId"])

    @override_settings(BANKCONNECT_ENABLE_SANDBOX=False)
    def test_the_sandbox_is_not_offered_where_it_is_switched_off(self):
        self.assertEqual([p["code"] for p in self.api_get("providers/").json()["providers"]], ["paystack", "monnify", "remita"])

    def test_each_provider_asks_only_for_what_the_school_was_given_by_it_and_never_for_a_settlement_account(self):
        by_code = {p["code"]: p for p in self.api_get("providers/").json()["providers"]}
        fields = {code: [f["name"] for f in p["credentialFields"]] for code, p in by_code.items()}
        self.assertEqual(fields["paystack"], ["secret_key"])
        self.assertEqual(fields["monnify"], ["api_key", "secret_key", "contract_code"])
        self.assertEqual(fields["remita"], ["merchant_id", "api_key", "service_type_id"])
        everything = json.dumps(by_code).lower()
        for banned in ("settlement", "account_number\"", "account_fingerprint", "is this your school"):
            self.assertNotIn(banned, everything)
        secret = {f["name"]: f["secret"] for p in by_code.values() for f in p["credentialFields"]}
        self.assertTrue(secret["secret_key"] and secret["api_key"])
        self.assertFalse(secret["merchant_id"] or secret["contract_code"] or secret["service_type_id"])

    def test_capabilities_and_webhook_instructions_are_stated_per_provider(self):
        by_code = {p["code"]: p for p in self.api_get("providers/").json()["providers"]}
        self.assertTrue(by_code["monnify"]["capabilities"]["requiresCustomerKyc"])
        self.assertFalse(by_code["remita"]["capabilities"]["supportsStaticAccounts"])
        self.assertFalse(by_code["paystack"]["capabilities"]["supportsAccountReactivation"])
        self.assertEqual(by_code["remita"]["webhook"]["verification"], "requery")
        self.assertEqual(by_code["paystack"]["webhook"]["verification"], "hmac_sha512")
        self.assertTrue(all(p["onboarding"] for p in by_code.values() if not p["isSandbox"]))

    @override_settings(BANKCONNECT_SECRET_KEYS=[])
    def test_it_says_when_secure_storage_is_not_set_up(self):
        self.assertFalse(self.api_get("providers/").json()["secureStorageReady"])

    def test_the_finance_office_can_look_but_not_manage_and_others_are_refused(self):
        seen = self.api_get("providers/", who=self.members["accountant"]).json()
        self.assertTrue(seen["permissions"]["canView"])
        self.assertFalse(seen["canManage"])
        for role in PROVIDER_ROLES:
            self.assertEqual(self.api_get("providers/", who=self.members[role]).status_code, 403, role)


class WhoMayManageTests(BankTestCase):
    def test_the_owner_may_do_everything_by_default(self):
        self.assertEqual(
            permissions.permissions_of(self.owner),
            {"canView": True, "canManageProviders": True, "canManagePolicy": True, "canPrepare": True, "canApprove": True},
        )

    def test_the_finance_office_views_but_holds_no_authority_without_a_duty(self):
        accountant = self.members["accountant"]
        self.assertEqual(
            permissions.permissions_of(accountant),
            {"canView": True, "canManageProviders": False, "canManagePolicy": False, "canPrepare": False, "canApprove": False},
        )

    def test_each_duty_grants_only_its_own_authority(self):
        accountant = self.members["accountant"]
        expected = {
            "finance.collection_provider_manage": "canManageProviders", "finance.collection_policy_manage": "canManagePolicy",
            "finance.collection_prepare": "canPrepare", "finance.collection_approve": "canApprove",
        }
        for duty, granted in expected.items():
            self.give_duty(accountant, duty)
            held = permissions.permissions_of(accountant)
            self.assertEqual([name for name, allowed in held.items() if allowed and name != "canView"], [granted], duty)

    def test_billing_authority_never_opens_the_provider_secrets(self):
        accountant = self.members["accountant"]
        self.give_duty(accountant, "finance.billing_authority")
        self.assertFalse(permissions.can_manage_providers(accountant))
        self.assertEqual(self.connect(who=accountant).status_code, 403)
        self.assertEqual(CollectionProviderConnection.objects.count(), 0)

    def test_the_earlier_bank_connections_duty_keeps_working_as_provider_authority_and_nothing_more(self):
        accountant = self.members["accountant"]
        self.give_duty(accountant, "finance.bank_connections")
        self.assertTrue(permissions.can_manage_providers(accountant))
        self.assertFalse(permissions.can_approve(accountant) or permissions.can_prepare(accountant) or permissions.can_manage_policy(accountant))

    def test_a_duty_that_is_not_active_grants_nothing(self):
        accountant = self.members["accountant"]
        self.give_duty(accountant, status="revoked")
        self.assertFalse(permissions.can_manage_providers(accountant))

    def test_a_duty_held_at_another_school_grants_nothing_here(self):
        # the assignment names a membership; a same-named record in another school is not this person
        self.give_duty(self.other_principal)
        self.assertFalse(permissions.can_manage_providers(self.members["principal"]))

    def test_people_outside_the_staff_never_hold_authority_whatever_a_stray_record_says(self):
        for role in ("parent", "student"):
            for duty in ALL_DUTIES:
                self.give_duty(self.members[role], duty)
            held = permissions.permissions_of(self.members[role])
            self.assertFalse(any(held.values()), role)

    def test_nobody_gets_authority_by_role_alone(self):
        for role in PROVIDER_ROLES:
            self.assertFalse(any(permissions.permissions_of(self.members[role]).values()), role)

    def test_a_person_may_only_connect_when_they_hold_the_provider_duty(self):
        accountant = self.members["accountant"]
        for duty in ALL_DUTIES[1:]:
            self.give_duty(accountant, duty)
            self.assertEqual(self.connect(who=accountant).status_code, 403, duty)
        self.give_duty(accountant, ALL_DUTIES[0])
        self.assertEqual(self.connect(who=accountant).status_code, 201)

    def test_every_management_action_is_refused_to_someone_without_the_duty(self):
        connection, _ = self.connected()
        accountant = self.members["accountant"]
        for action in ("test", "rename", "disable", "enable", "replace-credentials", "disconnect", "webhook-token", "activate"):
            response = self.api_post(f"connections/{connection['id']}/{action}/", {"label": "x", "credentials": {"sandbox_key": SANDBOX_KEY}}, who=accountant)
            self.assertEqual(response.status_code, 403, action)
        self.assertEqual(self.api_get(f"connections/{connection['id']}/webhook/", who=accountant).status_code, 403)
        self.assertEqual(self.api_get(f"connections/{connection['id']}/audit/", who=accountant).status_code, 403)
        self.assertEqual(self.row(connection).status, ConnectionStatus.CONNECTED)


class ConnectApiTests(BankTestCase):
    def test_connecting_verifies_the_credentials_and_keeps_only_safe_facts(self):
        response = self.connect(label="  Main   Sandbox ")
        self.assertEqual(response.status_code, 201, response.json())
        connection = response.json()["connection"]
        self.assertEqual(
            (connection["provider"], connection["environment"], connection["status"], connection["label"], connection["merchantName"]),
            ("sandbox", "test", "connected", "Main Sandbox", "Sandbox school"),
        )
        self.assertFalse(connection["isActiveProvider"])  # connecting never makes it the provider that issues accounts
        self.assertEqual(connection["webhookStatus"], "awaiting_event")
        self.assertIsNotNone(connection["lastVerifiedAt"])
        for old in ("bankName", "accountName", "accountMask", "purpose", "accountNumber", "settlementAccount"):
            self.assertNotIn(old, connection)
        self.assert_no_secrets(response.json())

    def test_the_credential_is_sealed_and_readable_only_by_the_server(self):
        row = self.row(self.connect().json()["connection"])
        self.assertNotIn(SANDBOX_KEY.encode(), bytes(row.sealed_credentials))
        self.assertEqual(self.secret_of(row)["sandbox_key"], SANDBOX_KEY)
        stored = " ".join(str(v) for v in (row.merchant_name, row.merchant_reference, row.label, row.provider_meta, row.provider_settings))
        self.assertNotIn(SANDBOX_KEY, stored)
        self.assertEqual(row.webhook_token_hash and len(row.webhook_token_hash), 64)

    def test_a_credential_sealed_for_one_connection_does_not_open_for_another(self):
        first = self.row(self.connect().json()["connection"])
        second = self.row(self.connect(who=self.other_owner, school=self.other_school).json()["connection"])
        second.sealed_credentials = first.sealed_credentials
        second.save()
        with self.assertRaises(vault.VaultError):
            self.secret_of(second)

    def test_the_list_shows_safe_facts_and_never_a_credential_or_webhook_token(self):
        connection, hook = self.connected()
        listed = self.api_get("connections/").json()
        self.assertEqual([c["id"] for c in listed["connections"]], [connection["id"]])
        self.assert_no_secrets(listed)
        self.assertNotIn(hook.split("/")[2], json.dumps(listed))

    def test_the_same_provider_cannot_be_connected_twice_but_can_after_disconnecting_and_a_second_school_may_have_it(self):
        first = self.connect().json()["connection"]
        again = self.connect(key="sandbox-another-key")
        self.assertEqual((again.status_code, again.json()["code"]), (400, "already_connected"))
        self.assertEqual(self.connect(who=self.other_owner, school=self.other_school).status_code, 201)
        self.api_post(f"connections/{first['id']}/disconnect/")
        self.assertEqual(self.connect().status_code, 201)
        self.assertEqual(CollectionProviderConnection.objects.filter(school=self.school).count(), 2)

    def test_the_database_itself_refuses_a_second_live_connection_to_a_real_provider(self):
        for provider in ("paystack", "monnify", "remita"):
            CollectionProviderConnection.objects.create(school=self.school, provider=provider, status=ConnectionStatus.CONNECTED)
            with self.assertRaises(IntegrityError), transaction.atomic():
                CollectionProviderConnection.objects.create(school=self.school, provider=provider, status=ConnectionStatus.CONNECTED)
            CollectionProviderConnection.objects.create(school=self.other_school, provider=provider, status=ConnectionStatus.CONNECTED)
            CollectionProviderConnection.objects.filter(school=self.school, provider=provider).update(status=ConnectionStatus.REVOKED)
            CollectionProviderConnection.objects.create(school=self.school, provider=provider, status=ConnectionStatus.CONNECTED)

    def test_an_unsupported_or_unknown_provider_is_refused_and_nothing_is_stored(self):
        for provider in ("gtbank", "uba", "opay", "open_banking", "nope", ""):
            response = self.api_post("connections/", {"provider": provider, "credentials": {"secret_key": "x"}})
            self.assertEqual((response.status_code, response.json()["code"]), (400, "unknown_provider"), provider)
        self.assertEqual(CollectionProviderConnection.objects.count(), 0)

    def test_bad_input_is_refused_without_echoing_it(self):
        cases = [
            ({"provider": "sandbox"}, "credentials_required"),
            ({"provider": "sandbox", "credentials": {}}, "credentials_required"),
            ({"provider": "sandbox", "credentials": {"sandbox_key": "wrong-SECRET-9"}}, "bad_credentials"),
            ({"provider": "sandbox", "credentials": {"sandbox_key": SANDBOX_KEY, "account_number": "0123456789"}}, "unexpected_field"),
            ({"provider": "sandbox", "environment": "live", "credentials": {"sandbox_key": SANDBOX_KEY}}, "invalid_environment"),
            ({"provider": "sandbox", "label": "x" * 81, "credentials": {"sandbox_key": SANDBOX_KEY}}, "invalid_label"),
            ({"provider": "sandbox", "credentials": {"sandbox_key": "x" * 600}}, "credentials_invalid"),
            ({"provider": "sandbox", "settings": {"bank": "x"}, "credentials": {"sandbox_key": SANDBOX_KEY}}, "unexpected_field"),
        ]
        for body, code in cases:
            response = self.api_post("connections/", body)
            self.assertEqual((response.status_code, response.json()["code"]), (400, code), body)
            self.assertNotIn("wrong-SECRET-9", json.dumps(response.json()))
        self.assertEqual(CollectionProviderConnection.objects.count(), 0)

    def test_a_failed_attempt_is_audited_without_what_was_typed(self):
        self.api_post("connections/", {"provider": "sandbox", "credentials": {"sandbox_key": "wrong-SECRET-9"}})
        event = BankAuditEvent.objects.get(kind="connect_failed")
        self.assertEqual(event.detail, {"provider": "sandbox", "code": "bad_credentials"})
        self.assertEqual(event.actor, self.owner)

    def test_a_failed_attempt_leaves_nothing_in_the_logs(self):
        with self.assertLogs("django.request", "WARNING") as captured:
            self.api_post("connections/", {"provider": "sandbox", "credentials": {"sandbox_key": "wrong-SECRET-9"}})
        self.assertNotIn("wrong-SECRET-9", "\n".join(captured.output))

    @override_settings(BANKCONNECT_SECRET_KEYS=[])
    def test_with_no_secure_storage_nothing_can_be_connected_and_the_provider_is_not_even_asked(self):
        response = self.connect()
        self.assertEqual((response.status_code, response.json()["code"]), (503, "secure_storage_unavailable"))
        self.assertEqual(CollectionProviderConnection.objects.count(), 0)
        self.assertEqual(BankAuditEvent.objects.count(), 0)

    def test_the_trail_records_the_connection_and_holds_no_secret(self):
        connection, _ = self.connected()
        self.assertEqual(self.audit_kinds(connection), ["provider_connected"])
        trail = self.api_get(f"connections/{connection['id']}/audit/")
        self.assertEqual(trail.status_code, 200)
        self.assert_no_secrets(trail.json(), [e.detail for e in BankAuditEvent.objects.all()])


class PaystackAndMonnifyConnectApiTests(BankTestCase):
    """The connect API for the real providers, with their own documented answers (no network)."""

    def with_provider(self, server):
        from ..providers.monnify import clear_token_cache
        from ..providers.transport import use_transport

        clear_token_cache()
        self.addCleanup(clear_token_cache)
        context = use_transport(server)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def test_paystack_is_connected_with_the_schools_own_secret_key_and_a_live_key_cannot_go_in_as_test(self):
        from .fake_transport import FakeTransport, ok

        server = FakeTransport().on("GET", "/dedicated_account/available_providers", ok({"status": True, "data": []}))
        self.with_provider(server)
        good = self.connect(provider="paystack", credentials={"secret_key": "sk_test_UNIQUE-PAYSTACK-1"})
        self.assertEqual(good.status_code, 201, good.json())
        self.assertEqual(good.json()["connection"]["settings"], {"preferred_bank": "test-bank"})
        self.assertNotIn("sk_test_UNIQUE-PAYSTACK-1", json.dumps(good.json()))
        bad = self.connect(provider="paystack", environment="live", credentials={"secret_key": "sk_test_UNIQUE-PAYSTACK-1"}, who=self.other_owner, school=self.other_school)
        self.assertEqual((bad.status_code, bad.json()["code"]), (400, "environment_mismatch"))
        self.assertEqual(server.called("GET", "/dedicated_account/available_providers"), 1)  # the mismatch never reached Paystack

    def test_a_key_paystack_refuses_is_reported_as_such_and_the_key_is_not_kept_or_shown(self):
        from .fake_transport import FakeTransport, ok

        server = FakeTransport().on("GET", "/dedicated_account/available_providers", ok({"status": False}, 401))
        self.with_provider(server)
        response = self.connect(provider="paystack", credentials={"secret_key": "sk_test_REFUSED-KEY"})
        self.assertEqual((response.status_code, response.json()["code"]), (400, "bad_credentials"))
        self.assertNotIn("REFUSED-KEY", json.dumps(response.json()))
        self.assertNotIn("REFUSED-KEY", json.dumps([e.detail for e in BankAuditEvent.objects.all()]))
        self.assertEqual(CollectionProviderConnection.objects.count(), 0)

    def test_monnify_is_connected_with_its_three_credentials_and_only_a_masked_contract_is_shown(self):
        from .fake_transport import FakeTransport
        from .test_monnify import login

        server = FakeTransport().on("POST", "/api/v1/auth/login", login())
        self.with_provider(server)
        creds = {"api_key": "MK_TEST_UNIQUEKEY", "secret_key": "UNIQUE-MONNIFY-SECRET", "contract_code": "7059707855"}
        response = self.connect(provider="monnify", credentials=creds)
        self.assertEqual(response.status_code, 201, response.json())
        connection = response.json()["connection"]
        self.assertEqual(connection["merchantReference"], "****7855")
        shown = json.dumps(response.json())
        for private in ("MK_TEST_UNIQUEKEY", "UNIQUE-MONNIFY-SECRET", "7059707855"):
            self.assertNotIn(private, shown)
        self.assertEqual(self.secret_of(self.row(connection))["contract_code"], "7059707855")

    def test_remita_live_is_refused_until_the_operator_has_configured_its_address(self):
        from .fake_transport import FakeTransport

        self.with_provider(FakeTransport())
        response = self.connect(provider="remita", environment="live", credentials={"merchant_id": "2547916", "api_key": "K", "service_type_id": "4430731"})
        self.assertEqual((response.status_code, response.json()["code"]), (400, "live_not_configured"))


class ReplaceCredentialsTests(BankTestCase):
    def setUp(self):
        super().setUp()
        self.connection, self.hook = self.connected()

    def replace(self, key="sandbox-REPLACEMENT-KEY", **kw):
        return self.api_post(f"connections/{self.connection['id']}/replace-credentials/", {"credentials": {"sandbox_key": key}}, **kw)

    def test_new_credentials_are_verified_sealed_and_the_old_ones_are_gone(self):
        response = self.replace()
        self.assertEqual(response.status_code, 200, response.json())
        row = self.row(self.connection)
        self.assertEqual(self.secret_of(row)["sandbox_key"], "sandbox-REPLACEMENT-KEY")
        self.assertNotIn(SANDBOX_KEY.encode(), bytes(row.sealed_credentials))
        self.assertNotIn("REPLACEMENT-KEY", json.dumps(response.json()))
        self.assertEqual(self.audit_kinds(self.connection), ["provider_connected", "credentials_replaced"])
        self.assertNotIn("REPLACEMENT", json.dumps([e.detail for e in BankAuditEvent.objects.all()]))

    def test_the_webhook_address_survives_a_credential_change(self):
        self.replace()
        setup = self.api_get(f"connections/{self.connection['id']}/webhook/").json()["webhook"]
        self.assertEqual(setup["path"], self.hook)

    def test_credentials_the_provider_refuses_change_nothing_and_are_audited_without_being_kept(self):
        response = self.replace("not-a-sandbox-key-BAD")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "bad_credentials"))
        self.assertEqual(self.secret_of(self.row(self.connection))["sandbox_key"], SANDBOX_KEY)
        self.assertNotIn("BAD", json.dumps([e.detail for e in BankAuditEvent.objects.all()]))
        self.assertIn("credentials_replace_failed", self.audit_kinds(self.connection) + [e.kind for e in BankAuditEvent.objects.all()])

    def test_credentials_for_a_different_merchant_are_refused(self):
        row = self.row(self.connection)
        row.merchant_reference = "****OLD1"
        row.save()
        response = self.replace()
        self.assertEqual((response.status_code, response.json()["code"]), (400, "different_merchant"))
        self.assertEqual(self.secret_of(row)["sandbox_key"], SANDBOX_KEY)

    def test_a_connection_that_needed_new_credentials_is_working_again_after_replacing_them(self):
        row = self.row(self.connection)
        row.status, row.last_error_code = ConnectionStatus.NEEDS_REAUTH, "bad_credentials"
        row.save()
        self.replace()
        row.refresh_from_db()
        self.assertEqual((row.status, row.last_error_code), (ConnectionStatus.CONNECTED, ""))

    def test_a_disconnected_provider_cannot_have_credentials_put_back_into_it(self):
        self.api_post(f"connections/{self.connection['id']}/disconnect/")
        self.assertEqual(self.replace().json()["code"], "wrong_status")
        self.assertEqual(bytes(self.row(self.connection).sealed_credentials), b"")


class LifecycleTests(BankTestCase):
    def setUp(self):
        super().setUp()
        self.connection, _ = self.connected()
        self.id = self.connection["id"]

    def act(self, action, body=None, **kw):
        return self.api_post(f"connections/{self.id}/{action}/", body, **kw)

    def test_a_working_connection_tests_well_and_a_broken_one_is_marked_as_needing_new_credentials(self):
        self.assertTrue(self.act("test").json()["test"]["ok"])
        self.reseal(self.row(self.connection), {"sandbox_key": ""})
        result = self.act("test").json()
        self.assertFalse(result["test"]["ok"])
        self.assertEqual((result["test"]["code"], result["connection"]["status"]), ("bad_credentials", "needs_reauth"))
        self.assertEqual(result["connection"]["lastErrorCode"], "bad_credentials")

    def test_an_unopenable_credential_is_reported_not_crashed(self):
        row = self.row(self.connection)
        row.sealed_credentials = b"garbage"
        row.save()
        result = self.act("test").json()
        self.assertEqual((result["test"]["ok"], result["test"]["code"]), (False, "vault_error"))

    def test_rename_is_audited_and_bounded(self):
        self.assertEqual(self.act("rename", {"label": "Front desk"}).json()["connection"]["label"], "Front desk")
        self.assertEqual(self.act("rename", {"label": "x" * 81}).json()["code"], "invalid_label")
        self.assertIn("provider_renamed", self.audit_kinds(self.connection))

    def test_a_provider_that_is_not_active_can_be_disabled_and_enabled_again_only_after_proving_it_still_works(self):
        self.assertEqual(self.act("disable").json()["connection"]["status"], "disabled")
        self.assertEqual(self.act("disable").json()["code"], "not_live")
        self.assertEqual(self.act("test").status_code, 200)  # a disabled provider can still be checked
        enabled = self.act("enable").json()
        self.assertEqual((enabled["connection"]["status"], enabled["test"]["ok"]), ("connected", True))
        self.assertEqual(self.act("enable").json()["code"], "not_disabled")

    def test_enabling_a_provider_whose_credentials_no_longer_work_says_so(self):
        self.act("disable")
        self.reseal(self.row(self.connection), {"sandbox_key": ""})
        enabled = self.act("enable").json()
        self.assertFalse(enabled["test"]["ok"])
        self.assertEqual(enabled["connection"]["status"], "needs_reauth")

    def test_disconnecting_wipes_the_credential_and_keeps_the_history(self):
        result = self.act("disconnect").json()["connection"]
        self.assertEqual(result["status"], "revoked")
        row = self.row(self.connection)
        self.assertEqual((bytes(row.sealed_credentials), row.webhook_token_hash), (b"", ""))
        self.assertIsNotNone(row.disconnected_at)
        self.assertEqual(self.act("disconnect").json()["connection"]["status"], "revoked")  # idempotent
        self.assertEqual(self.act("rename", {"label": "x"}).json()["code"], "closed")
        self.assertEqual(self.act("test").json()["code"], "not_live")
        self.assertIn("provider_disconnected", self.audit_kinds(self.connection))

    def test_every_action_on_another_schools_connection_is_a_plain_404(self):
        for action in ("test", "rename", "disable", "enable", "replace-credentials", "disconnect", "webhook-token", "activate"):
            response = self.api_post(f"connections/{self.id}/{action}/", {"label": "x", "credentials": {"sandbox_key": SANDBOX_KEY}}, who=self.other_owner, school=self.other_school)
            self.assertEqual(response.status_code, 404, action)
        self.assertEqual(self.api_get(f"connections/{self.id}/webhook/", who=self.other_owner, school=self.other_school).status_code, 404)
        self.assertEqual(self.api_get(f"connections/{self.id}/audit/", who=self.other_owner, school=self.other_school).status_code, 404)
        self.assertEqual(self.row(self.connection).status, ConnectionStatus.CONNECTED)

    def test_another_schools_owner_cannot_use_their_membership_to_reach_this_school(self):
        self.client.force_authenticate(self.other_owner.user)
        self.assertEqual(self.client.get(self.path("connections/")).status_code, 403)
        self.assertEqual(self.client.post(self.path("connections/"), {"provider": "sandbox", "credentials": {"sandbox_key": SANDBOX_KEY}}, format="json").status_code, 403)

    def test_each_school_lists_only_its_own_connections(self):
        other, _ = self.connected(who=self.other_owner, school=self.other_school)
        mine = [c["id"] for c in self.api_get("connections/").json()["connections"]]
        theirs = [c["id"] for c in self.api_get("connections/", who=self.other_owner, school=self.other_school).json()["connections"]]
        self.assertEqual((mine, theirs), ([self.id], [other["id"]]))


class ActiveProviderTests(BankTestCase):
    def setUp(self):
        super().setUp()
        self.connection, _ = self.connected()
        self.id = self.connection["id"]

    def activate(self, connection_id=None, **kw):
        return self.api_post(f"connections/{connection_id or self.id}/activate/", **kw)

    def test_the_first_connected_provider_is_chosen_as_active_and_it_is_audited(self):
        response = self.activate()
        self.assertEqual(response.status_code, 200, response.json())
        self.assertTrue(response.json()["connection"]["isActiveProvider"])
        self.assertEqual(self.api_get("providers/").json()["activeConnectionId"], self.id)
        self.assertIn("active_provider_set", self.audit_kinds(self.connection))
        self.assertTrue(self.activate().json()["connection"]["isActiveProvider"])  # idempotent

    def test_once_a_provider_is_active_another_is_never_made_active_directly(self):
        self.activate()
        self.reseal_second = None
        other = CollectionProviderConnection.objects.create(
            school=self.school, provider="paystack", environment="test", status=ConnectionStatus.CONNECTED, is_sandbox=False
        )
        response = self.activate(str(other.id))
        self.assertEqual((response.status_code, response.json()["code"]), (400, "use_switch"))
        other.refresh_from_db()
        self.assertFalse(other.is_active_provider)
        self.assertTrue(self.row(self.connection).is_active_provider)

    def test_only_a_connected_provider_can_be_made_active(self):
        for status in (ConnectionStatus.DISABLED, ConnectionStatus.NEEDS_REAUTH, ConnectionStatus.ERROR):
            row = self.row(self.connection)
            row.status = status
            row.save()
            self.assertEqual(self.activate().json()["code"], "not_connected", status)

    def test_the_active_provider_can_be_neither_disabled_nor_disconnected(self):
        self.activate()
        self.assertEqual(self.api_post(f"connections/{self.id}/disable/").json()["code"], "is_active_provider")
        self.assertEqual(self.api_post(f"connections/{self.id}/disconnect/").json()["code"], "is_active_provider")
        self.assertEqual(self.row(self.connection).status, ConnectionStatus.CONNECTED)

    def test_a_provider_with_live_family_accounts_can_be_neither_disabled_nor_disconnected(self):
        from apps.receivables import families
        from apps.receivables.models import AccountOrigin, AccountStatus, FamilyCollectionAccount

        family = families.create_family(self.school, display_name="Bello family")
        FamilyCollectionAccount.objects.create(
            school=self.school, family=family, provider="sandbox", connection=self.row(self.connection), origin=AccountOrigin.PROVIDER,
            account_number="9000000001", status=AccountStatus.ACTIVE,
        )
        self.assertEqual(self.api_post(f"connections/{self.id}/disable/").json()["code"], "has_live_accounts")
        self.assertEqual(self.api_post(f"connections/{self.id}/disconnect/").json()["code"], "has_live_accounts")

    def test_the_database_allows_only_one_active_provider_per_school_and_only_one_in_use(self):
        self.activate()
        other = CollectionProviderConnection.objects.create(school=self.school, provider="paystack", status=ConnectionStatus.CONNECTED)
        with self.assertRaises(IntegrityError), transaction.atomic():
            other.is_active_provider = True
            other.save()
        disabled = CollectionProviderConnection.objects.create(school=self.school, provider="monnify", status=ConnectionStatus.DISABLED)
        with self.assertRaises(IntegrityError), transaction.atomic():
            disabled.is_active_provider = True
            disabled.save()

    def test_a_school_can_have_its_own_active_provider_independent_of_another(self):
        other, _ = self.connected(who=self.other_owner, school=self.other_school)
        self.activate()
        self.assertEqual(self.api_post(f"connections/{other['id']}/activate/", who=self.other_owner, school=self.other_school).status_code, 200)

    def test_set_active_stands_down_the_old_one_so_the_rule_is_never_broken_even_for_an_instant(self):
        from .. import provider_connections

        first = self.row(self.connection)
        second = CollectionProviderConnection.objects.create(school=self.school, provider="paystack", status=ConnectionStatus.CONNECTED)
        provider_connections.set_active(first)
        provider_connections.set_active(second, kind="provider_switched")
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.is_active_provider, second.is_active_provider), (False, True))
        self.assertIn("provider_switched", [e.kind for e in BankAuditEvent.objects.filter(school=self.school)])


class WebhookSetupTests(BankTestCase):
    def setUp(self):
        super().setUp()
        self.connection, self.hook = self.connected()
        self.id = self.connection["id"]

    def setup_view(self, **kw):
        return self.api_get(f"connections/{self.id}/webhook/", **kw)

    def test_a_manager_is_told_where_to_put_the_address_and_it_is_not_called_active_yet(self):
        body = self.setup_view().json()["webhook"]
        self.assertEqual(body["path"], self.hook)
        self.assertTrue(self.hook.startswith("bank-webhooks/sandbox/"))
        self.assertEqual((body["status"], body["confirmedAt"], body["mode"]), ("awaiting_event", None, "dashboard"))
        self.assertEqual(body["verification"], "hmac_sha512")

    def test_only_a_verified_event_makes_the_webhook_active(self):
        from .. import sandbox_tools

        self.assertEqual(self.setup_view().json()["webhook"]["status"], "awaiting_event")
        body, _ = sandbox_tools.signed_webhook(self.row(self.connection), external_transaction_id="X1")
        self.client.force_authenticate(None)
        self.client.post(f"/api/v1/{self.hook}", data=body, content_type="application/json", HTTP_X_SANDBOX_SIGNATURE="0" * 64)
        self.assertEqual(self.row(self.connection).webhook_status, "awaiting_event")  # a forged one confirms nothing
        sandbox_tools.deliver(self.row(self.connection), external_transaction_id="X2")
        row = self.row(self.connection)
        self.assertEqual(row.webhook_status, "active")
        self.assertIsNotNone(row.webhook_confirmed_at)
        self.assertIn("webhook_confirmed", self.audit_kinds(self.connection))

    def test_a_new_address_replaces_the_old_and_the_webhook_waits_for_an_event_again(self):
        from .. import sandbox_tools

        sandbox_tools.deliver(self.row(self.connection), external_transaction_id="X1")
        renewed = self.api_post(f"connections/{self.id}/webhook-token/").json()["webhook"]
        self.assertNotEqual(renewed["path"], self.hook)
        self.assertEqual((renewed["status"], renewed["confirmedAt"]), ("awaiting_event", None))
        self.assertIn("webhook_address_renewed", self.audit_kinds(self.connection))
        self.assertNotIn(self.hook.split("/")[2], json.dumps([e.detail for e in BankAuditEvent.objects.all()]))

    def test_a_disconnected_provider_has_no_webhook_to_show(self):
        self.api_post(f"connections/{self.id}/disconnect/")
        self.assertEqual(self.setup_view().json()["code"], "closed")


class VaultTests(SimpleTestCase):
    CONTEXT = {"school": "s1", "connection": "c1"}

    def test_a_secret_round_trips_and_the_stored_bytes_do_not_contain_it(self):
        v = vault.FernetVault([Fernet.generate_key().decode()])
        blob = v.seal(self.CONTEXT, {"api_key": "super-secret-value"})
        self.assertNotIn(b"super-secret-value", blob)
        self.assertEqual(v.open(self.CONTEXT, blob), {"api_key": "super-secret-value"})

    def test_a_blob_copied_to_another_school_or_connection_does_not_open(self):
        v = vault.FernetVault([Fernet.generate_key().decode()])
        blob = v.seal(self.CONTEXT, {"api_key": "x"})
        for other in ({"school": "s2", "connection": "c1"}, {"school": "s1", "connection": "c2"}):
            with self.assertRaises(vault.VaultError):
                v.open(other, blob)

    def test_a_tampered_or_foreign_blob_does_not_open_and_the_error_holds_no_secret(self):
        v = vault.FernetVault([Fernet.generate_key().decode()])
        blob = v.seal(self.CONTEXT, {"api_key": "hunter2-secret"})
        with self.assertRaises(vault.VaultError) as raised:
            v.open(self.CONTEXT, blob[:-4] + b"AAAA")
        self.assertNotIn("hunter2", str(raised.exception))
        with self.assertRaises(vault.VaultError):
            vault.FernetVault([Fernet.generate_key().decode()]).open(self.CONTEXT, blob)

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
    def test_exactly_paystack_monnify_and_remita_can_be_connected_and_all_do_something_real(self):
        providers = registry.all_providers()
        self.assertEqual([p.code for p in providers], ["paystack", "monnify", "remita"])
        for info in providers:
            self.assertTrue(info.implemented, info.code)
            self.assertEqual(info.connection_type, "collection_provider")
            self.assertTrue(info.capabilities.supports_family_collection_accounts and info.capabilities.supports_webhooks, info.code)
        for legacy in ("gtbank", "uba", "access", "zenith", "firstbank", "opay", "moniepoint", "open_banking", "legacy_bank"):
            self.assertIsNone(registry.get_connector(legacy), legacy)

    @override_settings(BANKCONNECT_ENABLE_SANDBOX=False)
    def test_the_sandbox_is_absent_unless_switched_on(self):
        self.assertIsNone(registry.get_connector("sandbox"))

    @override_settings(BANKCONNECT_ENABLE_SANDBOX=True)
    def test_the_sandbox_appears_when_switched_on_and_is_marked_as_test_data(self):
        connector = registry.get_connector("sandbox")
        self.assertTrue(connector.info.is_sandbox)

    def test_a_connector_only_does_what_it_declares(self):
        connector = registry.get_connector("remita")
        with self.assertRaises(NotSupported):
            connector.deactivate_collection_account({}, account_ref="x", environment="test", settings={})


class AuditTests(BankTestCase):
    def test_anything_that_looks_like_a_secret_is_dropped_from_the_trail(self):
        event = audit.record(
            self.school, "provider_connected", actor=self.owner,
            provider="paystack", secret_key="AAA", api_key="BBB", access_token="CCC", password="DDD",
            nested={"authorization": "EEE", "kept": "yes"}, note="x" * 500,
        )
        blob = json.dumps(event.detail)
        for leaked in ("AAA", "BBB", "CCC", "DDD", "EEE"):
            self.assertNotIn(leaked, blob)
        self.assertEqual(event.detail["provider"], "paystack")
        self.assertEqual(event.detail["nested"], {"kept": "yes"})
        self.assertEqual(len(event.detail["note"]), 200)

    def test_the_trail_cannot_be_rewritten_or_deleted_through_the_model(self):
        event = audit.record(self.school, "provider_connected", actor=self.owner)
        event.kind = "changed"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()
