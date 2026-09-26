"""The HTTP surface: who may call what, that no account number or credential ever comes back, that another school's data is invisible, that a payer sees
only their own mandates, and the whole maker-checker flow through the API."""

import json

from apps.bankconnect.providers.transport import use_transport
from apps.bankconnect.tests.fake_transport import FakeTransport, ok

from .. import consent
from ..models import DirectDebitMandate
from .base import ACCOUNT, MANAGE, PROVIDER, MandateTestCase
from .test_remita import BASE as REMITA_BASE
from .test_remita import CREDS as REMITA_CREDS


class ApiCase(MandateTestCase):
    def path(self, tail, school=None):
        return f"/api/v1/schools/{(school or self.school).id}/mandates/{tail}"

    def get(self, tail, who=None, school=None):
        self.client.force_authenticate((who or self.owner).user)
        return self.client.get(self.path(tail, school))

    def post(self, tail, body=None, who=None, school=None):
        self.client.force_authenticate((who or self.owner).user)
        return self.client.post(self.path(tail, school), body or {}, format="json")

    def start_body(self, family, **over):
        body = {
            "familyId": str(family.id), "payerId": str(self.payer_of(family).id), "connectionId": str(self.connection.id), "bankCode": "058",
            "accountNumber": ACCOUNT, "maximumAmountMinor": 50_000_000, "consentRoute": "provider",
        }
        body.update(over)
        return body

    def assert_no_account(self, response):
        self.assertNotIn(ACCOUNT, response.content.decode())


class ProvidersApiTests(ApiCase):
    def test_the_owner_sees_the_mandate_providers_and_no_active_one(self):
        body = self.get("providers/").json()
        self.assertEqual([p["code"] for p in body["providers"]], ["remita", "lendsqr", "sandbox"])
        self.assertTrue(body["permissions"]["canManageProviders"] and body["secureStorageReady"])
        remita = body["providers"][0]
        self.assertEqual([f["name"] for f in remita["credentialFields"]], ["merchant_id", "service_type_id", "api_key", "api_token"])
        self.assertFalse(remita["liveAvailability"]["live"])
        self.assertIn("UAT", remita["liveAvailability"]["why"])
        lendsqr = body["providers"][1]
        self.assertIn("licensed", lendsqr["liveAvailability"]["why"])  # never a claim that a school is eligible
        self.assertFalse(lendsqr["capabilities"]["supportsManualDebit"])
        self.assertNotIn("isActive", json.dumps(body))

    def test_connecting_through_the_api_returns_no_credential_and_a_school_can_have_both(self):
        server = FakeTransport().on("POST", REMITA_BASE + "/status", ok({"statuscode": "074"}))
        with use_transport(server):
            response = self.post("connections/", {"provider": "remita", "environment": "test", "label": "Remita", "credentials": dict(REMITA_CREDS)})
        self.assertEqual(response.status_code, 201)
        shown = response.content.decode()
        for secret in (REMITA_CREDS["api_key"], REMITA_CREDS["api_token"]):
            self.assertNotIn(secret, shown)
        listed = self.get("connections/").json()
        self.assertEqual({c["provider"] for c in listed["connections"]}, {"sandbox", "remita"})
        self.assertNotIn(REMITA_CREDS["api_key"], json.dumps(listed))

    def test_only_a_provider_manager_can_connect_and_a_viewer_can_only_look(self):
        body = {"provider": "sandbox", "environment": "test", "credentials": {"sandbox_key": "sandbox-x"}}
        for who in (self.maker, self.checker, self.manager, self.members["teacher"]):
            self.assertEqual(self.post("connections/", body, who=who).status_code, 403, who.role)
        self.give_duties(self.manager, MANAGE, PROVIDER)
        self.assertEqual(self.post("connections/", body, who=self.manager).status_code, 400)  # allowed to try; already connected
        self.assertEqual(self.get("connections/", who=self.maker).status_code, 200)  # holders of a mandate duty can look
        self.assertEqual(self.get("connections/", who=self.members["teacher"]).status_code, 403)
        self.assertEqual(self.get("connections/", who=self.members["parent"]).status_code, 403)

    def test_another_schools_connection_is_a_404_on_every_action(self):
        for tail in (f"connections/{self.connection.id}/test/", f"connections/{self.connection.id}/disable/", f"connections/{self.connection.id}/replace-credentials/"):
            self.assertEqual(self.post(tail, {"credentials": {"sandbox_key": "sandbox-y"}}, who=self.other_owner, school=self.other_school).status_code, 404, tail)

    def test_unauthenticated_calls_are_refused(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(self.path("providers/")).status_code, (401, 403))


class MandatesApiTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.family = self.make_family("Bello", owes=280_000)

    def test_a_mandate_is_started_listed_and_shown_and_the_account_number_never_comes_back(self):
        response = self.post("mandates/", self.start_body(self.family), who=self.manager)
        self.assertEqual(response.status_code, 201, response.content)
        self.assert_no_account(response)
        mandate = response.json()["mandate"]
        self.assertEqual((mandate["accountMask"], mandate["bankCode"], mandate["status"], mandate["provider"]), ("****6789", "058", "pending_activation", "sandbox"))
        self.assertNotIn("sealed", json.dumps(mandate).lower())
        for tail in ("mandates/", f"mandates/{mandate['id']}/", f"mandates/?family={self.family.id}"):
            self.assert_no_account(self.get(tail, who=self.manager))
        listed = self.get("mandates/", who=self.maker).json()
        self.assertEqual([m["id"] for m in listed["mandates"]], [mandate["id"]])
        self.assertEqual(listed["counts"]["pending_activation"], 1)

    def test_only_a_manager_can_start_one_and_a_bad_request_is_a_400_with_a_code(self):
        for who in (self.maker, self.checker, self.members["teacher"]):
            self.assertEqual(self.post("mandates/", self.start_body(self.family), who=who).status_code, 403)
        bad = self.post("mandates/", self.start_body(self.family, accountNumber="123"), who=self.manager)
        self.assertEqual((bad.status_code, bad.json()["code"]), (400, "invalid_account_number"))
        self.assertNotIn("123", bad.json()["message"])
        worse = self.post("mandates/", self.start_body(self.family, startDate="not-a-date"), who=self.manager)
        self.assertEqual(worse.json()["code"], "invalid_dates")

    def test_a_mandate_of_another_school_is_a_404_and_the_list_shows_only_this_schools(self):
        mandate = self.start_mandate(self.family)
        self.assertEqual(self.get(f"mandates/{mandate.id}/", who=self.other_owner, school=self.other_school).status_code, 404)
        self.assertEqual(self.get("mandates/", who=self.other_owner, school=self.other_school).json()["mandates"], [])
        self.assertEqual(self.post(f"mandates/{mandate.id}/cancel/", who=self.other_owner, school=self.other_school).status_code, 404)

    def test_the_actions_need_the_manage_duty_and_do_what_they_say(self):
        mandate = self.active_mandate(self.family)
        for action in ("suspend", "cancel", "primary", "reactivate"):
            self.assertEqual(self.post(f"mandates/{mandate.id}/{action}/", who=self.maker).status_code, 403, action)
        suspended = self.post(f"mandates/{mandate.id}/suspend/", who=self.manager)
        self.assertEqual(suspended.json()["mandate"]["status"], "suspended")
        self.assertEqual(self.post(f"mandates/{mandate.id}/reactivate/", who=self.manager).json()["mandate"]["status"], "active")
        self.assertEqual(self.post(f"mandates/{mandate.id}/refresh/", who=self.maker).status_code, 200)  # anyone who can look may ask where it stands
        cancelled = self.post(f"mandates/{mandate.id}/cancel/", {"reason": "Changed banks"}, who=self.manager)
        self.assertEqual(cancelled.json()["mandate"]["status"], "cancelled")

    def test_the_family_payers_are_listed_without_contact_details(self):
        response = self.get(f"families/{self.family.id}/payers/", who=self.manager)
        (payer,) = response.json()["payers"]
        self.assertEqual((payer["name"], payer["hasEmail"], payer["hasPhone"], payer["hasAppAccount"]), ("Bello Parent", True, True, False))
        self.assertNotIn("phone\":", response.content.decode())
        self.assertEqual(self.get(f"families/{self.family.id}/payers/", who=self.members["teacher"]).status_code, 403)

    def test_the_debit_ready_filter_and_the_status_filter(self):
        self.active_mandate(self.family)
        pending = self.make_family("Sani", owes=1_000)
        self.start_mandate(pending, account="0987654321")
        self.assertEqual(len(self.get("mandates/?debitReady=1").json()["mandates"]), 1)
        self.assertEqual(len(self.get("mandates/?debitReady=0").json()["mandates"]), 1)
        self.assertEqual(len(self.get("mandates/?status=pending_activation").json()["mandates"]), 1)
        self.assertEqual(self.get("mandates/?family=nope").status_code, 400)


class PayerApiTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.family = self.make_family("Bello")
        self.parent = self.make_parent(self.family)
        self.other_family = self.make_family("Sani")
        self.other_parent = self.make_parent(self.other_family)
        self.mandate = self.start_mandate(self.family, route="payer_app")

    def test_a_payer_sees_only_their_own_mandate_with_the_words_they_are_asked_to_agree_to(self):
        response = self.get("my-mandates/", who=self.parent)
        (mine,) = response.json()["mandates"]
        self.assertEqual((mine["id"], mine["status"], mine["consentRequired"], mine["schoolName"]), (str(self.mandate.id), "pending_consent", True, "BrightGate"))
        self.assertIn("Bello Parent", mine["consentText"])
        self.assertEqual(mine["consentTextHash"], consent.text_hash(mine["consentText"]))
        self.assert_no_account(response)
        self.assertNotIn("providerCustomerRef", mine)
        self.assertEqual(self.get("my-mandates/", who=self.other_parent).json()["mandates"], [])

    def test_a_payer_cannot_see_or_touch_someone_elses_mandate_and_staff_are_not_payers(self):
        for who in (self.other_parent,):
            self.assertEqual(self.get(f"my-mandates/{self.mandate.id}/", who=who).status_code, 404)
            self.assertEqual(self.post(f"my-mandates/{self.mandate.id}/consent/", {"accepted": True, "consentTextHash": "x"}, who=who).status_code, 404)
            self.assertEqual(self.post(f"my-mandates/{self.mandate.id}/cancel/", who=who).status_code, 404)
        for who in (self.owner, self.manager, self.maker):
            self.assertEqual(self.get("my-mandates/", who=who).status_code, 403)

    def test_the_payer_authorises_it_through_the_api_and_a_stale_or_negative_answer_is_refused(self):
        shown = self.get(f"my-mandates/{self.mandate.id}/", who=self.parent).json()["mandate"]
        no = self.post(f"my-mandates/{self.mandate.id}/consent/", {"accepted": False, "consentTextHash": shown["consentTextHash"]}, who=self.parent)
        self.assertEqual((no.status_code, no.json()["code"]), (400, "consent_not_given"))
        stale = self.post(f"my-mandates/{self.mandate.id}/consent/", {"accepted": True, "consentTextHash": "0" * 64}, who=self.parent)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "stale_consent"))
        done = self.post(f"my-mandates/{self.mandate.id}/consent/", {"accepted": True, "consentTextHash": shown["consentTextHash"]}, who=self.parent)
        self.assertEqual((done.status_code, done.json()["mandate"]["status"]), (200, "pending_activation"))
        self.assertEqual(done.json()["mandate"]["consent"]["channel"], "payer_app")

    def test_the_payer_activates_with_a_one_time_password_and_can_always_cancel(self):
        shown = self.get(f"my-mandates/{self.mandate.id}/", who=self.parent).json()["mandate"]
        self.post(f"my-mandates/{self.mandate.id}/consent/", {"accepted": True, "consentTextHash": shown["consentTextHash"]}, who=self.parent)
        asked = self.post(f"my-mandates/{self.mandate.id}/activation-request/", who=self.parent)
        self.assertEqual(asked.json()["fields"][0]["name"], "OTP")
        wrong = self.post(f"my-mandates/{self.mandate.id}/activation-confirm/", {"answers": {"OTP": "9999"}}, who=self.parent)
        self.assertEqual((wrong.status_code, wrong.json()["code"]), (400, "activation_refused"))
        right = self.post(f"my-mandates/{self.mandate.id}/activation-confirm/", {"answers": {"OTP": "1234"}}, who=self.parent)
        self.assertEqual(right.json()["mandate"]["status"], "active")
        self.assertNotIn("1234", right.content.decode())
        cancelled = self.post(f"my-mandates/{self.mandate.id}/cancel/", {"reason": "I changed my mind"}, who=self.parent)
        self.assertEqual(cancelled.json()["mandate"]["status"], "cancelled")
        self.assertEqual(DirectDebitMandate.objects.get(pk=self.mandate.pk).status, "cancelled")


class DebitBatchApiTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.bello = self.make_family("Bello", owes=280_000)
        self.sani = self.make_family("Sani", owes=100_000)
        self.active_mandate(self.bello)
        self.active_mandate(self.sani, account="0987654321")

    def create_batch(self):
        response = self.post("debit-batches/", {"sessionId": str(self.session.id), "termId": str(self.term1.id), "title": "Term one fees"}, who=self.maker)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def test_the_whole_flow_over_http_with_a_maker_and_a_different_checker(self):
        created = self.create_batch()
        batch = created["batch"]
        self.assertEqual((batch["status"], batch["totalItems"], created["summary"]["families"]), ("draft", 0, 2))
        items = self.get(f"debit-batches/{batch['id']}/items/", who=self.maker).json()
        self.assertEqual({i["familyName"]: (i["eligibilityStatus"], i["proposedDebitMinor"]) for i in items["items"]}, {"Bello family": ("eligible", 28_000_000), "Sani family": ("eligible", 10_000_000)})
        self.assertTrue(all("accountMask" in i and ACCOUNT not in json.dumps(i) for i in items["items"]))
        selected = self.post(f"debit-batches/{batch['id']}/selection/", {"selectAllEligible": True, "version": items["version"]}, who=self.maker).json()["batch"]
        self.assertEqual((selected["totalItems"], selected["totalAmountMinor"]), (2, 38_000_000))
        submitted = self.post(f"debit-batches/{batch['id']}/submit/", {"snapshotHash": selected["snapshotHash"], "version": selected["version"]}, who=self.maker).json()["batch"]
        self.assertEqual(submitted["status"], "pending_approval")
        self.assertEqual(self.post(f"debit-batches/{batch['id']}/approve/", {"snapshotHash": submitted["snapshotHash"]}, who=self.maker).status_code, 403)  # the maker
        self.assertEqual(self.post(f"debit-batches/{batch['id']}/approve/", {"snapshotHash": submitted["snapshotHash"]}, who=self.manager).status_code, 403)  # no duty
        stale = self.post(f"debit-batches/{batch['id']}/approve/", {"snapshotHash": "0" * 64}, who=self.checker)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "stale_approval"))
        approved = self.post(f"debit-batches/{batch['id']}/approve/", {"snapshotHash": submitted["snapshotHash"]}, who=self.checker).json()["batch"]
        self.assertEqual((approved["status"], approved["approvedSnapshotHash"]), ("approved", submitted["snapshotHash"]))
        started = self.post(f"debit-batches/{batch['id']}/start/", who=self.maker)
        self.assertEqual(started.status_code, 200, started.content)
        from .. import jobs

        jobs.drain()
        progress = self.get(f"debit-batches/{batch['id']}/progress/", who=self.maker).json()["progress"]
        self.assertEqual((progress["status"], progress["success"], progress["done"]), ("completed", 2, True))
        transactions = self.get("transactions/", who=self.manager).json()["transactions"]
        self.assertEqual((len(transactions), {t["status"] for t in transactions}, all(t["settled"] for t in transactions)), (2, {"success"}, True))
        overview = self.get("overview/", who=self.manager).json()
        self.assertEqual((overview["debits"]["collectedMinor"], overview["debits"]["count"], overview["batches"]["waitingForApproval"]), (38_000_000, 2, 0))
        self.assertEqual({p["provider"] for p in overview["providers"]}, {"sandbox"})

    def test_a_maker_who_holds_only_the_mandate_duty_can_choose_a_session_and_term(self):
        body = self.get("periods/", who=self.maker).json()
        (session,) = [s for s in body["sessions"] if s["id"] == str(self.session.id)]
        self.assertEqual({t["name"] for t in session["terms"]}, {self.term1.name, self.term2.name})
        self.assertIn("current", body)
        self.assertEqual(self.get("periods/", who=self.members["teacher"]).status_code, 403)

    def test_only_a_maker_can_prepare_a_batch_and_a_rejection_needs_a_reason(self):
        for who in (self.checker, self.manager, self.members["teacher"]):
            self.assertEqual(self.post("debit-batches/", {"sessionId": str(self.session.id)}, who=who).status_code, 403)
        batch = self.create_batch()["batch"]
        self.post(f"debit-batches/{batch['id']}/selection/", {"selectAllEligible": True}, who=self.maker)
        latest = self.get(f"debit-batches/{batch['id']}/", who=self.maker).json()["batch"]
        self.post(f"debit-batches/{batch['id']}/submit/", {"snapshotHash": latest["snapshotHash"]}, who=self.maker)
        no_reason = self.post(f"debit-batches/{batch['id']}/reject/", {"reason": "no"}, who=self.checker)
        self.assertEqual((no_reason.status_code, no_reason.json()["code"]), (400, "reason_required"))
        rejected = self.post(f"debit-batches/{batch['id']}/reject/", {"reason": "Please check Bello's term first"}, who=self.checker).json()["batch"]
        self.assertEqual((rejected["status"], rejected["rejectionReason"]), ("rejected", "Please check Bello's term first"))
        events = self.get(f"debit-batches/{batch['id']}/events/", who=self.maker).json()["events"]
        self.assertIn("rejected", [e["kind"] for e in events])

    def test_a_person_can_lower_but_not_raise_an_amount_over_http(self):
        batch = self.create_batch()["batch"]
        item = self.get(f"debit-batches/{batch['id']}/items/", who=self.maker).json()["items"][0]
        too_much = self.post(f"debit-batches/{batch['id']}/items/{item['id']}/amount/", {"amountMinor": item["proposedDebitMinor"] + 1}, who=self.maker)
        self.assertEqual((too_much.status_code, too_much.json()["code"]), (400, "amount_too_high"))
        lowered = self.post(f"debit-batches/{batch['id']}/items/{item['id']}/amount/", {"amountMinor": 1_000_000}, who=self.maker)
        self.assertEqual(lowered.status_code, 200)

    def test_another_schools_batch_is_a_404_everywhere(self):
        batch = self.create_batch()["batch"]
        for tail in ("", "items/", "events/", "progress/", "failed/"):
            self.assertEqual(self.get(f"debit-batches/{batch['id']}/{tail}", who=self.other_owner, school=self.other_school).status_code, 404, tail)
        for action in ("selection", "submit", "approve", "reject", "cancel", "start", "retry"):
            self.assertEqual(self.post(f"debit-batches/{batch['id']}/{action}/", who=self.other_owner, school=self.other_school).status_code, 404, action)
        self.assertEqual(self.get("debit-batches/", who=self.other_owner, school=self.other_school).json()["batches"], [])

    def test_a_batch_that_went_stale_is_a_409_and_is_back_with_the_maker(self):
        batch = self.create_batch()["batch"]
        self.post(f"debit-batches/{batch['id']}/selection/", {"selectAllEligible": True}, who=self.maker)
        latest = self.get(f"debit-batches/{batch['id']}/", who=self.maker).json()["batch"]
        self.post(f"debit-batches/{batch['id']}/submit/", {"snapshotHash": latest["snapshotHash"]}, who=self.maker)
        self.pay_manually(self.bello, 10_000)
        response = self.post(f"debit-batches/{batch['id']}/approve/", {"snapshotHash": latest["snapshotHash"]}, who=self.checker)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "batch_changed"))
        self.assertEqual(self.get(f"debit-batches/{batch['id']}/", who=self.maker).json()["batch"]["status"], "draft")

    def test_viewers_of_mandates_can_read_batches_but_others_cannot(self):
        batch = self.create_batch()["batch"]
        self.assertEqual(self.get(f"debit-batches/{batch['id']}/", who=self.checker).status_code, 200)
        for who in (self.members["teacher"], self.members["parent"], self.members["student"]):
            self.assertEqual(self.get(f"debit-batches/{batch['id']}/", who=who).status_code, 403)
