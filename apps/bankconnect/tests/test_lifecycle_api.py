from datetime import timedelta

from django.utils import timezone

from ..models import BankConnection
from .base import ACCOUNT, SANDBOX_KEY, BankTestCase

GOOD = {"sandbox_key": SANDBOX_KEY, "account_number": ACCOUNT}


class TestConnectionTests(BankTestCase):
    def test_a_working_connection_passes(self):
        connection, _ = self.connected()
        response = self.api_post(f"connections/{connection['id']}/test/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["test"], {"ok": True, "code": "", "message": ""})
        self.assertEqual(response.json()["connection"]["status"], "connected")
        self.assertIn("test_passed", self.audit_kinds(connection))

    def test_a_credential_the_bank_no_longer_accepts_needs_reconnecting(self):
        connection, _ = self.connected()
        self.reseal(self.row(connection), {"account_number": ACCOUNT})  # no key any more
        body = self.api_post(f"connections/{connection['id']}/test/").json()
        self.assertFalse(body["test"]["ok"])
        self.assertEqual(body["test"]["code"], "bad_credentials")
        self.assertEqual((body["connection"]["status"], body["connection"]["lastErrorCode"]), ("needs_reauth", "bad_credentials"))
        self.assertIn("test_failed", self.audit_kinds(connection))

    def test_a_provider_that_reports_a_different_account_is_an_error_not_a_success(self):
        connection, _ = self.connected()
        self.reseal(self.row(connection), {"sandbox_key": SANDBOX_KEY, "account_number": "9999999999"})
        body = self.api_post(f"connections/{connection['id']}/test/").json()
        self.assertEqual((body["test"]["code"], body["connection"]["status"]), ("account_changed", "error"))

    def test_a_credential_that_cannot_be_opened_is_reported_not_crashed_on(self):
        connection, _ = self.connected()
        row = self.row(connection)
        row.sealed_credentials = b"garbage"
        row.save()
        body = self.api_post(f"connections/{connection['id']}/test/").json()
        self.assertEqual((body["test"]["code"], body["connection"]["status"]), ("vault_error", "error"))

    def test_a_credential_copied_from_another_school_does_not_open(self):
        ours, _ = self.connected()
        theirs, _ = self.connected(who=self.other_owner, school=self.other_school)
        mine, other = self.row(ours), self.row(theirs)
        other.sealed_credentials = mine.sealed_credentials
        other.save()
        body = self.api_post(f"connections/{theirs['id']}/test/", who=self.other_owner, school=self.other_school).json()
        self.assertEqual(body["test"]["code"], "vault_error")

    def test_an_expiring_token_is_renewed_before_it_is_used(self):
        state = self.api_post("connections/authorize/", {"provider": "sandbox", "redirectUri": "https://app.example/cb"}).json()["state"]
        created = self.api_post("connections/", {"provider": "sandbox", "authorizationCode": "sandbox-approved", "state": state}).json()["connection"]
        self.api_post(f"connections/{created['id']}/confirm/")
        row = self.row(created)
        old_token = self.secret_of(row)["access_token"]
        row.token_expires_at = timezone.now() + timedelta(seconds=30)
        row.save()
        self.assertTrue(self.api_post(f"connections/{created['id']}/test/").json()["test"]["ok"])
        row.refresh_from_db()
        self.assertNotEqual(self.secret_of(row)["access_token"], old_token)
        self.assertGreater(row.token_expires_at, timezone.now() + timedelta(minutes=30))

    def test_a_pending_or_disconnected_account_cannot_be_tested(self):
        pending = self.connect().json()["connection"]
        self.assertEqual(self.api_post(f"connections/{pending['id']}/test/").json()["code"], "not_live")
        connection, _ = self.connected(account="0123456780")
        self.api_post(f"connections/{connection['id']}/disconnect/")
        self.assertEqual(self.api_post(f"connections/{connection['id']}/test/").json()["code"], "not_live")


class RenameTests(BankTestCase):
    def test_the_purpose_and_name_can_change(self):
        connection, _ = self.connected()
        response = self.api_post(f"connections/{connection['id']}/rename/", {"purpose": "transport", "label": "  Bus   fees "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["connection"]["purpose"], response.json()["connection"]["label"]), ("transport", "Bus fees"))
        self.assertIn("renamed", self.audit_kinds(connection))

    def test_only_what_is_sent_changes_and_nonsense_is_refused(self):
        connection, _ = self.connected()
        url = f"connections/{connection['id']}/rename/"
        kept = self.api_post(url, {"label": "Renamed"}).json()["connection"]
        self.assertEqual((kept["purpose"], kept["label"]), ("tuition", "Renamed"))
        self.assertEqual(self.api_post(url, {}).json()["code"], "nothing_to_change")
        self.assertEqual(self.api_post(url, {"purpose": "holiday"}).json()["code"], "invalid_purpose")

    def test_a_disconnected_account_cannot_be_renamed(self):
        connection, _ = self.connected()
        self.api_post(f"connections/{connection['id']}/disconnect/")
        self.assertEqual(self.api_post(f"connections/{connection['id']}/rename/", {"label": "x"}).json()["code"], "closed")


class DisableEnableTests(BankTestCase):
    def test_disabling_stops_it_and_enabling_proves_it_still_works(self):
        connection, _ = self.connected()
        disabled = self.api_post(f"connections/{connection['id']}/disable/")
        self.assertEqual(disabled.json()["connection"]["status"], "disabled")
        self.assertEqual(self.api_post(f"connections/{connection['id']}/disable/").json()["code"], "not_live")
        enabled = self.api_post(f"connections/{connection['id']}/enable/").json()
        self.assertEqual(enabled["connection"]["status"], "connected")
        self.assertTrue(enabled["test"]["ok"])
        self.assertEqual(self.api_post(f"connections/{connection['id']}/enable/").json()["code"], "not_disabled")

    def test_enabling_with_a_dead_credential_lands_in_needs_reconnecting(self):
        connection, _ = self.connected()
        self.api_post(f"connections/{connection['id']}/disable/")
        self.reseal(self.row(connection), {"account_number": ACCOUNT})
        enabled = self.api_post(f"connections/{connection['id']}/enable/").json()
        self.assertFalse(enabled["test"]["ok"])
        self.assertEqual(enabled["connection"]["status"], "needs_reauth")

    def test_a_disabled_account_can_still_be_tested_without_becoming_live(self):
        connection, _ = self.connected()
        self.api_post(f"connections/{connection['id']}/disable/")
        self.reseal(self.row(connection), {"account_number": ACCOUNT})
        body = self.api_post(f"connections/{connection['id']}/test/").json()
        self.assertFalse(body["test"]["ok"])
        self.assertEqual(body["connection"]["status"], "disabled")


class RotateAndReconnectTests(BankTestCase):
    def test_a_new_credential_for_the_same_account_replaces_the_old(self):
        connection, _ = self.connected()
        before = bytes(self.row(connection).sealed_credentials)
        response = self.api_post(
            f"connections/{connection['id']}/rotate/",
            {"credentials": {"sandbox_key": "sandbox-NEW-KEY-555", "account_number": ACCOUNT}},
        )
        self.assertEqual(response.status_code, 200, response.json())
        row = self.row(connection)
        self.assertNotEqual(bytes(row.sealed_credentials), before)
        self.assertEqual(self.secret_of(row)["sandbox_key"], "sandbox-NEW-KEY-555")
        self.assertEqual(response.json()["connection"]["status"], "connected")
        self.assertNotIn("sandbox-NEW-KEY-555", str(response.json()))
        self.assertIn("credentials_rotated", self.audit_kinds(connection))
        for event in self.audit_events():
            self.assertNotIn("NEW-KEY", str(event.detail))

    def audit_events(self):
        from ..models import BankAuditEvent

        return BankAuditEvent.objects.all()

    def test_a_credential_for_a_different_account_is_refused_and_changes_nothing(self):
        connection, _ = self.connected()
        before = bytes(self.row(connection).sealed_credentials)
        response = self.api_post(
            f"connections/{connection['id']}/rotate/",
            {"credentials": {"sandbox_key": "sandbox-x", "account_number": "0000000000"}},
        )
        self.assertEqual((response.status_code, response.json()["code"]), (400, "different_account"))
        self.assertEqual(bytes(self.row(connection).sealed_credentials), before)

    def test_reconnecting_restores_an_account_that_stopped_working(self):
        connection, _ = self.connected()
        self.reseal(self.row(connection), {"account_number": ACCOUNT})
        self.api_post(f"connections/{connection['id']}/test/")
        self.assertEqual(self.row(connection).status, "needs_reauth")
        response = self.api_post(f"connections/{connection['id']}/reconnect/", {"credentials": GOOD})
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["connection"]["status"], "connected")
        self.assertEqual(response.json()["connection"]["lastErrorCode"], "")
        self.assertIn("reconnected", self.audit_kinds(connection))

    def test_an_account_that_is_working_does_not_need_reconnecting(self):
        connection, _ = self.connected()
        response = self.api_post(f"connections/{connection['id']}/reconnect/", {"credentials": GOOD})
        self.assertEqual((response.status_code, response.json()["code"]), (400, "wrong_status"))

    def test_a_pending_or_disconnected_account_cannot_be_rotated(self):
        pending = self.connect().json()["connection"]
        self.assertEqual(self.api_post(f"connections/{pending['id']}/rotate/", {"credentials": GOOD}).json()["code"], "wrong_status")
        connection, _ = self.connected(account="0123456780")
        self.api_post(f"connections/{connection['id']}/disconnect/")
        self.assertEqual(self.api_post(f"connections/{connection['id']}/rotate/", {"credentials": GOOD}).json()["code"], "wrong_status")


class DisconnectTests(BankTestCase):
    def test_disconnecting_wipes_the_credential_and_keeps_the_record(self):
        connection, _ = self.connected()
        response = self.api_post(f"connections/{connection['id']}/disconnect/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["connection"]["status"], "revoked")
        self.assertTrue(response.json()["providerRevoked"])
        row = self.row(connection)
        self.assertEqual(bytes(row.sealed_credentials), b"")
        self.assertEqual(row.webhook_token_hash, "")
        self.assertIsNotNone(row.disconnected_at)
        self.assertIn("disconnected", self.audit_kinds(connection))

    def test_it_is_safe_to_do_twice_and_is_audited_once(self):
        connection, _ = self.connected()
        self.api_post(f"connections/{connection['id']}/disconnect/")
        again = self.api_post(f"connections/{connection['id']}/disconnect/")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.audit_kinds(connection).count("disconnected"), 1)

    def test_it_works_even_when_the_credential_can_no_longer_be_opened(self):
        connection, _ = self.connected()
        row = self.row(connection)
        row.sealed_credentials = b"garbage"
        row.save()
        response = self.api_post(f"connections/{connection['id']}/disconnect/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["providerRevoked"])
        self.assertEqual(bytes(self.row(connection).sealed_credentials), b"")

    def test_a_pending_account_the_person_did_not_recognise_can_be_cancelled(self):
        pending = self.connect().json()["connection"]
        self.assertEqual(self.api_post(f"connections/{pending['id']}/disconnect/").json()["connection"]["status"], "revoked")


class WebhookTokenTests(BankTestCase):
    def test_a_new_address_replaces_the_old_and_is_shown_once(self):
        connection, first = self.connected()
        response = self.api_post(f"connections/{connection['id']}/webhook-token/")
        self.assertEqual(response.status_code, 200)
        second = response.json()["webhook"]["path"]
        self.assertNotEqual(first, second)
        from .. import identifiers

        self.assertEqual(self.row(connection).webhook_token_hash, identifiers.hash_token(second.split("/")[2]))
        self.assertNotIn(second.split("/")[2], str(self.api_get("connections/").json()))

    def test_a_pending_account_has_no_address_yet(self):
        pending = self.connect().json()["connection"]
        self.assertEqual(self.api_post(f"connections/{pending['id']}/webhook-token/").json()["code"], "no_webhooks")


class LifecyclePermissionTests(BankTestCase):
    def test_every_action_needs_the_power_to_manage(self):
        connection, _ = self.connected()
        finance = self.members["accountant"]
        for action in ("confirm", "test", "rename", "disable", "enable", "rotate", "reconnect", "disconnect", "webhook-token"):
            for who in (finance, self.members["principal"], self.members["teacher"]):
                response = self.api_post(f"connections/{connection['id']}/{action}/", {"label": "x"}, who=who)
                self.assertEqual(response.status_code, 403, (action, who.role))
        self.assertEqual(self.row(connection).status, "connected")

    def test_someone_given_the_duty_can_manage(self):
        connection, _ = self.connected()
        finance = self.members["accountant"]
        self.give_duty(finance)
        self.assertEqual(self.api_post(f"connections/{connection['id']}/disable/", who=finance).status_code, 200)
        self.assertEqual(BankConnection.objects.get().status, "disabled")
