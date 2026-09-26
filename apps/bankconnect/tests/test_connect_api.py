from django.test import override_settings

from .. import identifiers
from ..models import BankAuditEvent, BankConnection
from .base import ACCOUNT, SANDBOX_KEY, BankTestCase


class ProvidersApiTests(BankTestCase):
    def test_the_owner_sees_every_provider_and_which_can_really_be_connected(self):
        response = self.api_get("providers/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        by_code = {p["code"]: p for p in body["providers"]}
        self.assertTrue(by_code["sandbox"]["available"])
        for code in ("gtbank", "uba", "zenith", "access", "firstbank", "moniepoint", "opay", "open_banking", "monnify", "paystack"):
            self.assertFalse(by_code[code]["available"], code)
            self.assertEqual(by_code[code]["productionStatus"], "pending_verified_documentation")
            self.assertFalse(any(by_code[code]["capabilities"].values()))
        self.assertTrue(by_code["sandbox"]["capabilities"]["supportsWebhooks"])
        self.assertTrue(body["canManage"])
        self.assertTrue(body["secureStorageReady"])

    @override_settings(BANKCONNECT_SECRET_KEYS=[])
    def test_it_says_when_secure_storage_is_not_set_up(self):
        self.assertFalse(self.api_get("providers/").json()["secureStorageReady"])

    @override_settings(BANKCONNECT_ENABLE_SANDBOX=False)
    def test_the_sandbox_is_not_offered_where_it_is_switched_off(self):
        codes = [p["code"] for p in self.api_get("providers/").json()["providers"]]
        self.assertNotIn("sandbox", codes)

    def test_the_finance_office_can_look_but_not_manage_and_others_are_refused(self):
        seen = self.api_get("providers/", who=self.members["accountant"])
        self.assertEqual(seen.status_code, 200)
        self.assertFalse(seen.json()["canManage"])
        for role in ("principal", "administrator", "teacher", "staff", "parent", "student", "driver"):
            self.assertEqual(self.api_get("providers/", who=self.members[role]).status_code, 403, role)


class ConnectApiTests(BankTestCase):
    def test_connecting_verifies_the_account_and_waits_for_confirmation(self):
        response = self.connect()
        self.assertEqual(response.status_code, 201, response.json())
        connection = response.json()["connection"]
        self.assertEqual(connection["status"], "pending")
        self.assertEqual(
            (connection["bankName"], connection["accountName"], connection["accountMask"], connection["purpose"], connection["label"]),
            ("Sandbox Bank", "SANDBOX SCHOOL ACCOUNT", "****6789", "tuition", "Tuition Collection"),
        )
        self.assertTrue(connection["isSandbox"])
        self.assert_no_secrets(response.json())

    def test_nothing_readable_is_stored_and_the_full_number_never_is(self):
        connection = self.row(self.connect().json()["connection"])
        self.assertNotIn(SANDBOX_KEY.encode(), bytes(connection.sealed_credentials))
        self.assertNotIn(ACCOUNT.encode(), bytes(connection.sealed_credentials))
        self.assertEqual(self.secret_of(connection)["sandbox_key"], SANDBOX_KEY)
        stored = " ".join(str(v) for v in (connection.bank_name, connection.account_name, connection.account_mask,
                                            connection.account_fingerprint, connection.label, connection.provider_meta))
        self.assertNotIn(ACCOUNT, stored)
        self.assertEqual(connection.account_fingerprint, identifiers.fingerprint(self.school.id, "sandbox", ACCOUNT))

    def test_the_same_account_at_two_schools_has_different_fingerprints(self):
        self.assertNotEqual(
            identifiers.fingerprint(self.school.id, "sandbox", ACCOUNT),
            identifiers.fingerprint(self.other_school.id, "sandbox", ACCOUNT),
        )

    def test_confirming_connects_it_and_shows_the_webhook_address_once(self):
        created = self.connect().json()["connection"]
        confirmed = self.api_post(f"connections/{created['id']}/confirm/")
        self.assertEqual(confirmed.status_code, 200)
        body = confirmed.json()
        self.assertEqual(body["connection"]["status"], "connected")
        self.assertTrue(body["connection"]["webhookConfigured"])
        path = body["webhook"]["path"]
        token = path.split("/")[2]
        row = self.row(created)
        self.assertEqual(row.webhook_token_hash, identifiers.hash_token(token))
        self.assertNotIn(token, row.webhook_token_hash)
        listed = self.api_get("connections/").json()
        self.assertNotIn("webhook", listed["connections"][0])
        self.assertNotIn(token, str(listed))
        self.assertEqual(self.api_post(f"connections/{created['id']}/confirm/").status_code, 400)

    def test_the_same_account_cannot_be_connected_twice_but_can_after_disconnecting(self):
        first = self.connect().json()["connection"]
        again = self.connect(key="sandbox-another-key")
        self.assertEqual(again.status_code, 400)
        self.assertEqual(again.json()["code"], "already_connected")
        self.api_post(f"connections/{first['id']}/disconnect/")
        self.assertEqual(self.connect().status_code, 201)

    def test_a_second_account_and_the_other_schools_own_copy_are_fine(self):
        self.assertEqual(self.connect().status_code, 201)
        self.assertEqual(self.connect(account="0123456780").status_code, 201)
        self.assertEqual(self.connect(who=self.other_owner, school=self.other_school).status_code, 201)
        self.assertEqual(BankConnection.objects.count(), 3)

    def test_a_bank_that_is_not_ready_says_so_and_nothing_is_stored(self):
        response = self.api_post("connections/", {"provider": "gtbank", "credentials": {"anything": "x"}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "pending_documentation")
        self.assertEqual(self.api_post("connections/", {"provider": "nope"}).json()["code"], "unknown_provider")
        self.assertEqual(BankConnection.objects.count(), 0)

    def test_bad_input_is_refused_without_echoing_it(self):
        cases = [
            ({"provider": "sandbox"}, "credentials_required"),
            ({"provider": "sandbox", "credentials": {"sandbox_key": "wrong-SECRET-9", "account_number": ACCOUNT}}, "bad_credentials"),
            ({"provider": "sandbox", "credentials": {"sandbox_key": SANDBOX_KEY}}, "credentials_required"),
            ({"provider": "sandbox", "credentials": {"sandbox_key": SANDBOX_KEY, "account_number": ACCOUNT, "extra": "x"}}, "unexpected_field"),
            ({"provider": "sandbox", "purpose": "holiday", "credentials": {"sandbox_key": SANDBOX_KEY, "account_number": ACCOUNT}}, "invalid_purpose"),
            ({"provider": "sandbox", "label": "x" * 81, "credentials": {"sandbox_key": SANDBOX_KEY, "account_number": ACCOUNT}}, "invalid_label"),
        ]
        for body, code in cases:
            response = self.api_post("connections/", body)
            self.assertEqual((response.status_code, response.json()["code"]), (400, code), body)
            self.assertNotIn("wrong-SECRET-9", str(response.json()))
        self.assertEqual(BankConnection.objects.count(), 0)

    def test_a_failed_attempt_is_audited_without_what_was_typed(self):
        self.api_post("connections/", {"provider": "sandbox", "credentials": {"sandbox_key": "wrong-SECRET-9", "account_number": ACCOUNT}})
        event = BankAuditEvent.objects.get(kind="connect_failed")
        self.assertEqual(event.detail, {"provider": "sandbox", "code": "bad_credentials"})
        self.assertEqual(event.actor, self.owner)

    def test_a_failed_attempt_leaves_nothing_in_the_logs(self):
        with self.assertLogs("django.request", "WARNING") as captured:
            self.api_post("connections/", {"provider": "sandbox", "credentials": {"sandbox_key": "wrong-SECRET-9", "account_number": ACCOUNT}})
        self.assertNotIn("wrong-SECRET-9", "\n".join(captured.output))
        self.assertNotIn(ACCOUNT, "\n".join(captured.output))

    @override_settings(BANKCONNECT_SECRET_KEYS=[])
    def test_with_no_secure_storage_nothing_can_be_connected(self):
        response = self.connect()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "secure_storage_unavailable")
        self.assertEqual(BankConnection.objects.count(), 0)

    def test_every_step_is_audited_and_the_trail_holds_no_secret(self):
        created = self.connect().json()["connection"]
        self.api_post(f"connections/{created['id']}/confirm/")
        kinds = self.audit_kinds(created)
        self.assertIn("connection_created", kinds)
        self.assertIn("connected", kinds)
        trail = self.api_get(f"connections/{created['id']}/audit/")
        self.assertEqual(trail.status_code, 200)
        self.assertEqual({e["kind"] for e in trail.json()["events"]}, {"connection_created", "connected"})
        self.assert_no_secrets(trail.json(), [e.detail for e in BankAuditEvent.objects.all()])


class AuthorizationFlowTests(BankTestCase):
    def begin(self, who=None, uri="https://app.example/callback"):
        return self.api_post("connections/authorize/", {"provider": "sandbox", "redirectUri": uri}, who=who)

    def test_the_person_is_sent_to_the_providers_own_page_and_no_password_is_asked(self):
        response = self.begin()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("state=", body["authorizationUrl"])
        completed = self.api_post(
            "connections/", {"provider": "sandbox", "authorizationCode": "sandbox-approved", "state": body["state"], "purpose": "transport"}
        )
        self.assertEqual(completed.status_code, 201, completed.json())
        connection = completed.json()["connection"]
        self.assertEqual((connection["status"], connection["purpose"]), ("pending", "transport"))
        self.assertIsNotNone(connection["tokenExpiresAt"])

    def test_the_state_must_be_valid_and_belong_to_the_person_who_started_it(self):
        state = self.begin().json()["state"]
        finance = self.members["accountant"]
        self.give_duty(finance)
        for who, code, given in (
            (self.owner, "sandbox-approved", state + "x"),
            (self.owner, "sandbox-approved", ""),
            (finance, "sandbox-approved", state),
        ):
            response = self.api_post("connections/", {"provider": "sandbox", "authorizationCode": code, "state": given}, who=who)
            self.assertEqual((response.status_code, response.json()["code"]), (400, "bad_state"))

    def test_a_wrong_code_is_refused(self):
        state = self.begin().json()["state"]
        response = self.api_post("connections/", {"provider": "sandbox", "authorizationCode": "guess", "state": state})
        self.assertEqual((response.status_code, response.json()["code"]), (400, "bad_credentials"))

    def test_the_return_address_must_be_safe(self):
        for uri in ("javascript:alert(1)", "http://evil.example/cb", "ftp://x.example/cb", "", "https://" + "a" * 600):
            response = self.begin(uri=uri)
            self.assertEqual((response.status_code, response.json()["code"]), (400, "invalid_redirect"), uri)
        for uri in ("https://app.example/cb", "http://localhost:5000/cb", "schoolos://bank/return"):
            self.assertEqual(self.begin(uri=uri).status_code, 200, uri)

    def test_only_someone_who_manages_accounts_may_start_one(self):
        self.assertEqual(self.begin(who=self.members["accountant"]).status_code, 403)
        self.give_duty(self.members["accountant"])
        self.assertEqual(self.begin(who=self.members["accountant"]).status_code, 200)


class ConnectPermissionTests(BankTestCase):
    def test_the_finance_office_needs_the_duty_to_connect(self):
        finance = self.members["accountant"]
        self.assertEqual(self.connect(who=finance).status_code, 403)
        self.give_duty(finance)
        created = self.connect(who=finance)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(BankConnection.objects.get().created_by, finance)

    def test_a_revoked_duty_takes_the_power_away_again(self):
        finance = self.members["accountant"]
        self.give_duty(finance)
        self.give_duty(finance, status="revoked")
        self.assertEqual(self.connect(who=finance).status_code, 403)

    def test_nobody_else_can_connect_or_list(self):
        for role in ("principal", "administrator", "teacher", "staff", "parent", "student", "driver"):
            self.assertEqual(self.connect(who=self.members[role]).status_code, 403, role)
            self.assertEqual(self.api_get("connections/", who=self.members[role]).status_code, 403, role)
        self.assertEqual(BankConnection.objects.count(), 0)

    def test_the_finance_office_can_list_and_read_the_trail(self):
        connection, _ = self.connected()
        finance = self.members["accountant"]
        self.assertEqual(self.api_get("connections/", who=finance).json()["canManage"], False)
        self.assertEqual(len(self.api_get("connections/", who=finance).json()["connections"]), 1)
        self.assertEqual(self.api_get(f"connections/{connection['id']}/audit/", who=finance).status_code, 200)

    def test_another_schools_owner_gets_nothing_of_ours(self):
        connection, _ = self.connected()
        theirs = self.api_get("connections/", who=self.other_owner, school=self.other_school).json()
        self.assertEqual(theirs["connections"], [])
        # Naming our school is refused outright; naming theirs finds nothing of ours.
        self.assertEqual(self.api_get("connections/", who=self.other_owner).status_code, 403)
        self.assertEqual(self.api_post("connections/", {"provider": "sandbox"}, who=self.other_owner).status_code, 403)
        for action in ("test", "disable", "disconnect", "confirm", "rotate", "rename", "webhook-token"):
            url = f"connections/{connection['id']}/{action}/"
            self.assertEqual(self.api_post(url, who=self.other_owner, school=self.other_school).status_code, 404, action)
        self.assertEqual(
            self.api_get(f"connections/{connection['id']}/audit/", who=self.other_owner, school=self.other_school).status_code, 404
        )
        self.assertEqual(self.row(connection).status, "connected")

    def test_a_membership_that_is_not_the_persons_own_is_refused(self):
        self.client.force_authenticate(self.owner.user)
        response = self.client.get(self.path("connections/") + f"?membership={self.members['teacher'].id}")
        self.assertEqual(response.status_code, 403)
