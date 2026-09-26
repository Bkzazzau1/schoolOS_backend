"""Smart Money Collection over HTTP: who may call what, the refusals the app can switch on, tenant isolation, the exports, and that no response
ever carries a credential or an identity number."""

import io
import json
import re
import zipfile
from datetime import timedelta
from xml.etree import ElementTree

from django.test import override_settings
from django.utils import timezone

from .. import batches
from .base import CollectTestCase


class ApiCase(CollectTestCase):
    def path(self, tail, school=None):
        return f"/api/v1/schools/{(school or self.school).id}/collections/{tail}"

    def get(self, tail, who=None, school=None, **params):
        self.client.force_authenticate((who or self.owner).user)
        return self.client.get(self.path(tail, school), params)

    def post(self, tail, body=None, who=None, school=None):
        self.client.force_authenticate((who or self.owner).user)
        return self.client.post(self.path(tail, school), body or {}, format="json")

    def patch(self, tail, body=None, who=None):
        self.client.force_authenticate((who or self.owner).user)
        return self.client.patch(self.path(tail), body or {}, format="json")

    def put(self, tail, body=None, who=None):
        self.client.force_authenticate((who or self.owner).user)
        return self.client.put(self.path(tail), body or {}, format="json")

    def create_batch(self, who=None, **body):
        response = self.post("batches/", {"sessionId": str(self.session.id), "termId": str(self.term1.id), **body}, who=who or self.maker)
        self.assertEqual(response.status_code, 201, response.json())
        return response.json()["batch"]

    def batch_path(self, batch, tail=""):
        return f"batches/{batch['id']}/{tail}"


class AccessTests(ApiCase):
    def test_looking_is_for_the_owner_finance_and_duty_holders_and_nobody_else(self):
        for tail in ("dashboard/", "policy/", "policy/overrides/", "switches/", "batches/"):
            self.assertEqual(self.get(tail).status_code, 200, tail)
            self.assertEqual(self.get(tail, who=self.members["accountant"]).status_code, 200, tail)
            self.assertEqual(self.get(tail, who=self.checker).status_code, 200, tail)  # holds a duty
            for role in ("teacher", "parent", "student", "driver", "staff"):
                self.assertEqual(self.get(tail, who=self.members[role]).status_code, 403, (tail, role))

    def test_another_school_is_a_closed_door(self):
        for tail in ("dashboard/", "policy/", "batches/"):
            self.assertEqual(self.get(tail, who=self.other_owner).status_code, 403, tail)
        batch = self.create_batch()
        self.assertEqual(self.get(self.batch_path(batch), who=self.other_owner, school=self.other_school).status_code, 404)
        self.assertEqual(self.post(self.batch_path(batch, "cancel/"), who=self.other_owner, school=self.other_school).status_code, 404)

    def test_nothing_needs_no_sign_in(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(self.path("dashboard/")).status_code, (401, 403))


class PolicyApiTests(ApiCase):
    def test_the_policy_and_its_choices_are_served_to_the_screen(self):
        body = self.get("policy/").json()["policy"]
        self.assertEqual(body["values"]["account_mode"], "static")
        self.assertIn("grace_then_close", [o["value"] for o in body["options"]["settlement_action"]])
        self.assertEqual(body["labels"]["settlement_action"], "When a family has paid")

    def test_changing_it_needs_authority_and_a_finished_policy(self):
        denied = self.patch("policy/", {"values": {"account_mode": "dynamic"}}, who=self.maker)
        self.assertEqual((denied.status_code, denied.json()["code"]), (403, "not_policy_manager"))
        unfinished = self.patch("policy/", {"values": {"settlement_action": "grace_then_close"}})
        self.assertEqual((unfinished.status_code, unfinished.json()["code"]), (400, "policy_incomplete"))
        done = self.patch("policy/", {"values": {"settlement_action": "grace_then_close", "grace_period_hours": 72}})
        self.assertEqual(done.json()["policy"]["values"]["grace_period_hours"], 72)

    def test_overrides_are_set_listed_shown_as_effective_and_removed(self):
        family = self.make_family("Alpha")
        made = self.post("policy/overrides/", {
            "scope": "family", "familyId": str(family.id), "values": {"account_mode": "dynamic"}, "reason": "Pays per term",
            "expiryKind": "end_of_term", "expiryTermId": str(self.term1.id),
        })
        self.assertEqual(made.status_code, 201, made.json())
        override = made.json()["override"]
        self.assertEqual((override["targetLabel"], override["expiryTerm"], override["active"]), ("Alpha family", "First Term", True))
        listed = self.get("policy/overrides/", scope="family").json()["overrides"]
        self.assertEqual([o["id"] for o in listed], [override["id"]])
        effective = self.get("policy/effective/", session=str(self.session.id), term=str(self.term1.id), family=str(family.id)).json()["effective"]
        self.assertEqual((effective["values"]["account_mode"], effective["sources"]["account_mode"]["scope"], effective["sources"]["settlement_action"]["scope"]), ("dynamic", "family", "school"))
        row = next(r for r in effective["description"]["fields"] if r["field"] == "account_mode")
        self.assertEqual((row["inherited"], row["label"]), (False, "Account type"))
        removed = self.post(f"policy/overrides/{override['id']}/remove/", {"reason": "done"})
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(self.get("policy/overrides/", scope="family").json()["overrides"], [])
        self.assertEqual(len(self.get("policy/overrides/", scope="family", history="1").json()["overrides"]), 1)

    def test_a_provider_cannot_be_chosen_by_a_family(self):
        family = self.make_family("Alpha")
        response = self.post("policy/overrides/", {"scope": "family", "familyId": str(family.id), "values": {"provider": "paystack"}, "reason": "x"})
        self.assertEqual((response.status_code, response.json()["code"]), (400, "provider_not_overridable"))

    def test_a_family_of_another_school_is_not_found(self):
        family = self.make_family("Alpha")
        response = self.post("policy/overrides/", {"scope": "family", "familyId": str(family.id), "values": {"account_mode": "dynamic"}, "reason": "x"}, who=self.other_owner, school=self.other_school)
        self.assertEqual(response.status_code, 404)


@override_settings(SMART_COLLECTION_INLINE_JOBS=True, SMART_COLLECTION_INLINE_SECONDS=30)
class BatchApiTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.a = self.make_family("Alpha")
        self.b = self.make_family("Bravo", owes=50_000)
        self.c = self.make_family("Nomail", email=None)

    def test_a_maker_prepares_a_batch_and_sees_it_with_what_they_may_do(self):
        batch = self.create_batch(title="Term one")
        self.assertEqual((batch["status"], batch["title"], batch["provider"]["code"], batch["period"]), ("draft", "Term one", "sandbox", "2026/2027 · First Term"))
        self.assertEqual((batch["totals"]["families"], batch["totals"]["selected"], batch["totals"]["collectionMinor"]), (3, 2, 150_000 * 100))
        self.assertEqual(batch["can"], {
            "edit": True, "submit": True, "approve": False, "reject": False, "start": False, "retry": False, "cancel": True, "overridePolicy": False,
        })
        self.assertEqual(batch["counts"]["missing_details"], {"total": 1, "selected": 0})
        self.assertEqual(batch["preparedBy"]["role"], "accountant")

    def test_a_checker_cannot_prepare_a_batch(self):
        response = self.post("batches/", {"sessionId": str(self.session.id)}, who=self.checker)
        self.assertEqual((response.status_code, response.json()["code"]), (403, "not_preparer"))

    def test_the_families_are_listed_by_bucket_with_their_figures(self):
        batch = self.create_batch()
        response = self.get(self.batch_path(batch, "items/"), bucket="missing_details")
        item = response.json()["items"][0]
        self.assertEqual((item["familyName"], item["eligibilityStatus"], item["selected"], item["missingDetails"]), ("Nomail family", "missing_details", False, ["the payer's email address"]))
        everything = self.get(self.batch_path(batch, "items/")).json()
        self.assertEqual(everything["buckets"], {"eligible": {"total": 2, "selected": 2}, "missing_details": {"total": 1, "selected": 0}})
        self.assertEqual(len(self.get(self.batch_path(batch, "items/"), q="alph").json()["items"]), 1)
        self.assertEqual(len(self.get(self.batch_path(batch, "items/"), limit=1).json()["items"]), 1)

    def test_selection_uses_the_version_the_screen_saw_and_a_stale_screen_gets_a_conflict_it_can_act_on(self):
        batch = self.create_batch()
        item = self.get(self.batch_path(batch, "items/"), q="Alpha").json()["items"][0]
        done = self.post(self.batch_path(batch, "selection/"), {"deselect": [item["id"]], "expectedVersion": batch["version"]}, who=self.maker)
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.json()["batch"]["totals"]["selected"], 1)
        stale = self.post(self.batch_path(batch, "selection/"), {"select": [item["id"]], "expectedVersion": batch["version"]}, who=self.maker)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "stale_preview"))
        self.assertEqual(stale.json()["version"], done.json()["batch"]["version"])

    def test_the_full_road_over_http(self):
        batch = self.create_batch()
        seen = self.get(self.batch_path(batch)).json()["batch"]
        submitted = self.post(self.batch_path(batch, "submit/"), {"expectedHash": seen["snapshotHash"], "expectedVersion": seen["version"]}, who=self.maker)
        self.assertEqual((submitted.status_code, submitted.json()["batch"]["status"]), (200, "pending_approval"))
        awaiting = self.get("batches/", who=self.checker, awaiting="1").json()["batches"]
        self.assertEqual([b["id"] for b in awaiting], [batch["id"]])
        self.assertEqual((awaiting[0]["can"]["approve"], awaiting[0]["isMaker"]), (True, False))
        self.assertEqual(self.get("batches/", who=self.maker, awaiting="1").json()["batches"], [])  # never waiting on the maker
        hash_seen = submitted.json()["batch"]["snapshotHash"]
        own = self.post(self.batch_path(batch, "approve/"), {"expectedHash": hash_seen}, who=self.maker)
        self.assertEqual((own.status_code, own.json()["code"]), (403, "not_approver"))  # the maker holds no approve duty at all
        approved = self.post(self.batch_path(batch, "approve/"), {"expectedHash": hash_seen}, who=self.checker)
        self.assertEqual((approved.status_code, approved.json()["batch"]["status"]), (200, "approved"))
        self.assertEqual(self.get("dashboard/").json()["accounts"]["familiesWithLiveAccount"], 0)  # nothing yet
        started = self.post(self.batch_path(batch, "start/"), who=self.maker)
        self.assertEqual(started.status_code, 200, started.json())
        final = started.json()["batch"]
        self.assertEqual((final["status"], final["totals"]["successful"], final["totals"]["failed"]), ("completed", 2, 0))
        progress = self.get(self.batch_path(batch, "progress/")).json()["progress"]
        self.assertEqual((progress["total"], progress["successful"], progress["finished"]), (2, 2, True))
        self.assertEqual(len(self.live_accounts(self.a)), 1)

    def test_a_rejection_needs_its_reason_over_http_and_returns_to_the_maker(self):
        batch = self.create_batch()
        self.post(self.batch_path(batch, "submit/"), {"expectedHash": batch["snapshotHash"]}, who=self.maker)
        no_reason = self.post(self.batch_path(batch, "reject/"), {"reason": ""}, who=self.checker)
        self.assertEqual((no_reason.status_code, no_reason.json()["code"]), (400, "reason_required"))
        rejected = self.post(self.batch_path(batch, "reject/"), {"reason": "The nomail family needs sorting first"}, who=self.checker)
        self.assertEqual(rejected.json()["batch"]["rejectionReason"], "The nomail family needs sorting first")
        mine = self.get("batches/", who=self.maker, mine="1", status="rejected").json()["batches"]
        self.assertEqual([b["id"] for b in mine], [batch["id"]])
        events = self.get(self.batch_path(batch, "events/")).json()["events"]
        self.assertEqual([e["kind"] for e in events][-2:], ["submitted", "rejected"])

    def test_approving_a_batch_that_changed_after_it_was_opened_is_a_conflict_and_withdraws_it(self):
        batch = self.create_batch()
        self.post(self.batch_path(batch, "submit/"), {"expectedHash": batch["snapshotHash"]}, who=self.maker)
        self.post("policy/overrides/", {"scope": "term", "termId": str(self.term1.id), "values": {"account_mode": "dynamic"}, "reason": ""})
        response = self.post(self.batch_path(batch, "approve/"), {"expectedHash": batch["snapshotHash"]}, who=self.checker)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "batch_changed"))
        self.assertEqual(self.get(self.batch_path(batch)).json()["batch"]["status"], "draft")

    def test_overriding_a_family_over_http_needs_a_reason_and_is_offered_only_where_the_policy_asks(self):
        family = self.make_family("Owing", owes=100_000)
        self.into_term_two()
        self.charge_family(family, [m.student for m in family.members.all()], 60_000, term=self.term2)
        made = self.post("batches/", {"sessionId": str(self.session.id), "termId": str(self.term2.id)}, who=self.maker).json()["batch"]
        item = self.get(self.batch_path(made, "items/"), q="Owing").json()["items"][0]
        self.assertEqual((item["eligibilityStatus"], item["previousArrearsMinor"], item["currentDueMinor"]), ("needs_override", 100_000 * 100, 60_000 * 100))
        blank = self.post(self.batch_path(made, f"items/{item['id']}/override/"), {"reason": ""}, who=self.maker)
        self.assertEqual((blank.status_code, blank.json()["code"]), (400, "reason_required"))
        done = self.post(self.batch_path(made, f"items/{item['id']}/override/"), {"reason": "Head teacher agreed"}, who=self.maker)
        self.assertEqual(done.status_code, 200, done.json())
        after = self.get(self.batch_path(made, "items/"), q="Owing").json()["items"][0]
        self.assertEqual((after["eligibilityOverride"], after["overrideReason"], after["selected"], after["overrideBy"]["role"]), (True, "Head teacher agreed", True, "accountant"))
        cleared = self.post(self.batch_path(made, f"items/{item['id']}/override-clear/"), who=self.maker)
        self.assertFalse(self.get(self.batch_path(made, "items/"), q="Owing").json()["items"][0]["selected"] or cleared.status_code != 200)

    def test_the_maker_can_set_a_policy_for_just_this_batch(self):
        batch = self.create_batch()
        done = self.post(self.batch_path(batch, "policy/"), {"values": {"account_mode": "dynamic"}, "reason": ""}, who=self.maker)
        self.assertEqual(done.status_code, 200, done.json())
        self.assertEqual(done.json()["batch"]["policy"]["account_mode"], "dynamic")
        self.assertEqual(done.json()["batch"]["policySources"]["account_mode"]["scope"], "batch")

    def test_failed_families_are_listed_and_can_be_retried_over_http(self):
        from apps.bankconnect.providers import base, sandbox

        sandbox.inject_fault(base.ProviderRejected("provider_rejected", "The provider did not accept the request."), match=self.b.code, times=1)
        batch = self.create_batch()
        self.post(self.batch_path(batch, "submit/"), {"expectedHash": batch["snapshotHash"]}, who=self.maker)
        self.post(self.batch_path(batch, "approve/"), {"expectedHash": batch["snapshotHash"]}, who=self.checker)
        done = self.post(self.batch_path(batch, "start/"), who=self.maker).json()["batch"]
        self.assertEqual((done["status"], done["totals"]["successful"], done["totals"]["failed"]), ("partially_successful", 1, 1))
        failed = self.get(self.batch_path(batch, "failed/")).json()
        self.assertEqual((failed["count"], failed["items"][0]["familyName"], failed["items"][0]["errorCode"]), (1, "Bravo family", "provider_rejected"))
        self.assertTrue(done["can"]["retry"])
        retried = self.post(self.batch_path(batch, "retry/"), {"itemIds": [failed["items"][0]["id"]]}, who=self.maker)
        self.assertEqual((retried.json()["retried"], retried.json()["batch"]["status"]), (1, "completed"))

    def test_a_retry_that_needs_a_fresh_approval_says_so(self):
        from apps.bankconnect.providers import base, sandbox

        sandbox.inject_fault(base.ProviderRejected("provider_rejected", "no"), match=self.b.code, times=1)
        batch = self.create_batch()
        self.post(self.batch_path(batch, "submit/"), {"expectedHash": batch["snapshotHash"]}, who=self.maker)
        self.post(self.batch_path(batch, "approve/"), {"expectedHash": batch["snapshotHash"]}, who=self.checker)
        self.post(self.batch_path(batch, "start/"), who=self.maker)
        self.set_policy(settlement_action="manual")
        again = self.post(self.batch_path(batch, "retry/"), who=self.maker).json()
        self.assertEqual((again["retried"], again["approvalNeeded"], again["batch"]["status"]), (0, True, "draft"))

    def test_a_batch_can_be_cancelled_and_its_history_is_kept(self):
        batch = self.create_batch()
        done = self.post(self.batch_path(batch, "cancel/"), {"reason": "Wrong term"}, who=self.maker)
        self.assertEqual(done.json()["batch"]["status"], "cancelled")
        self.assertEqual(self.get(self.batch_path(batch)).status_code, 200)

    def test_the_session_and_term_must_be_this_schools_and_agree(self):
        self.assertEqual(self.post("batches/", {"sessionId": "not-an-id"}, who=self.maker).json()["code"], "session_required")
        other = self.post("batches/", {"sessionId": str(self.session.id), "termId": "00000000-0000-0000-0000-000000000000"}, who=self.maker)
        self.assertEqual(other.json()["code"], "term_not_in_session")


class ExportTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.make_family("Alpha", owes=100_000)
        self.make_family("Brave & Sons <Ltd>", owes=50_000)
        self.make_family("Nomail", email=None)
        self.batch = self.create_batch(title="Term one")

    def download(self, fmt, **params):
        self.client.force_authenticate(self.checker.user)
        return self.client.get(self.path(self.batch_path(self.batch, "export/")), {"type": fmt, **params})

    def test_the_excel_export_is_a_valid_workbook_with_every_family_and_the_totals(self):
        response = self.download("xlsx")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertIn("collection-batch-", response["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            self.assertIsNone(z.testzip())
            self.assertTrue({"[Content_Types].xml", "xl/workbook.xml", "xl/styles.xml", "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"} <= set(z.namelist()))
            for name in z.namelist():
                ElementTree.fromstring(z.read(name))  # every part is well-formed XML, whatever a family is called
            sheet = z.read("xl/worksheets/sheet1.xml").decode()
        text = "".join(re.findall(r"<t[^>]*>([^<]*)</t>", sheet))
        self.assertIn("Alpha family", text)
        self.assertIn("Brave &amp; Sons &lt;Ltd&gt; family", sheet)
        self.assertIn("Payer details missing", text)
        numbers = [float(v) for v in re.findall(r"<v>([^<]*)</v>", sheet)]
        self.assertIn(100000.0, numbers)
        self.assertIn(150000.0, numbers)  # the total of the selected families

    def test_the_pdf_is_a_valid_document_naming_the_batch_and_its_fingerprint(self):
        response = self.download("pdf")
        self.assertEqual((response.status_code, response["Content-Type"]), (200, "application/pdf"))
        data = response.content
        self.assertTrue(data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"Alpha family", data)
        self.assertIn(b"Page 1 of 1", data)
        self.assertIn(self.batch["snapshotHash"][:16].encode(), data)
        xref = int(re.search(rb"startxref\n(\d+)", data).group(1))
        self.assertTrue(data[xref:].startswith(b"xref"))  # the cross-reference table is where the file says it is

    def test_a_long_batch_paginates_and_repeats_its_header(self):
        for i in range(70):
            self.make_family(f"Extra{i:02d}", owes=1_000)
        batches.refresh_preview(self.maker, self.batch["id"])
        data = self.download("pdf").content
        pages = int(re.search(rb"Page 1 of (\d+)", data).group(1))
        self.assertGreater(pages, 1)
        self.assertEqual(data.count(b"Eligibility"), pages)  # the column headings on every page

    def test_only_the_selected_families_can_be_exported(self):
        response = self.download("xlsx", selected="1")
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode()
        self.assertNotIn("Nomail family", sheet)

    def test_an_export_reads_what_is_stored_and_calls_no_provider(self):
        from apps.receivables.models import FamilyCollectionAccount

        self.download("pdf")
        self.download("xlsx")
        self.assertEqual((self.sandbox_accounts().count(), FamilyCollectionAccount.objects.count()), (0, 0))

    def test_an_unknown_format_and_another_school_are_refused(self):
        self.assertEqual(self.download("docx").json()["code"], "invalid_format")
        self.client.force_authenticate(self.other_owner.user)
        self.assertEqual(self.client.get(self.path(self.batch_path(self.batch, "export/"), self.other_school), {"type": "pdf"}).status_code, 404)


class SwitchAndDashboardApiTests(ApiCase):
    def test_a_switch_is_planned_reviewed_and_applied_only_when_asked(self):
        from apps.bankconnect.models import CollectionProviderConnection

        other = CollectionProviderConnection.objects.create(
            school=self.school, provider="paystack", environment="test", status="connected", merchant_name="Paystack school", webhook_status="active"
        )
        planned = self.post("switches/", {"toConnectionId": str(other.id), "scheduledFor": (timezone.now() - timedelta(minutes=1)).isoformat()})
        self.assertEqual(planned.status_code, 201, planned.json())
        switch = planned.json()["switch"]
        self.assertEqual((switch["status"], switch["current"]["provider"], switch["target"]["provider"], switch["canApply"]), ("ready_to_switch", "sandbox", "paystack", True))
        self.assertEqual(self.get("dashboard/").json()["activeProvider"]["provider"], "sandbox")  # ready is not applied
        detail = self.get(f"switches/{switch['id']}/").json()
        self.assertEqual(detail["review"]["affected"]["accounts"], 0)
        denied = self.post(f"switches/{switch['id']}/apply/", who=self.maker)
        self.assertEqual(denied.status_code, 403)
        applied = self.post(f"switches/{switch['id']}/apply/")
        self.assertEqual((applied.status_code, applied.json()["switch"]["status"]), (200, "applied"))
        self.assertEqual(self.get("dashboard/").json()["activeProvider"]["provider"], "paystack")
        self.assertEqual(self.get("switches/").json()["open"], None)

    def test_the_dashboard_gathers_the_whole_picture(self):
        self.make_family("Alpha")
        self.make_family("Bravo")
        batch = self.create_batch()
        self.post(self.batch_path(batch, "submit/"), {"expectedHash": batch["snapshotHash"]}, who=self.maker)
        body = self.get("dashboard/", who=self.checker).json()
        self.assertEqual(body["activeProvider"]["provider"], "sandbox")
        self.assertEqual(len(body["connectedProviders"]), 1)
        self.assertEqual(body["currentPeriod"]["term"]["name"], "First Term")
        self.assertEqual((body["accounts"]["activeFamilies"], body["accounts"]["familiesWithoutAccount"]), (2, 2))
        self.assertEqual((body["batches"]["pendingApproval"], len(body["pendingApprovals"])), (1, 1))
        self.assertEqual((body["failedGenerations"], body["scheduledSwitch"]), ({"families": 0, "batches": 0}, None))
        self.assertTrue(body["permissions"]["canApprove"] and not body["permissions"]["canPrepare"])
        self.assertIn("reconciliation", body)
        self.assertEqual(body["policy"]["values"]["account_mode"], "static")

    def test_a_maker_sees_their_own_pending_batches_but_none_awaiting_them(self):
        self.make_family("Alpha")
        batch = self.create_batch()
        self.post(self.batch_path(batch, "submit/"), {"expectedHash": batch["snapshotHash"]}, who=self.maker)
        self.assertEqual(self.get("dashboard/", who=self.maker).json()["pendingApprovals"], [])


class SecrecyTests(ApiCase):
    BVN = "22222222222"

    def test_the_identity_number_is_written_and_never_returned(self):
        family = self.make_family("Alpha")
        put = self.put(f"families/{family.id}/payer-identity/", {"bvn": self.BVN}, who=self.maker)
        self.assertEqual((put.status_code, put.json()), (200, {"identity": {"hasBvn": True, "hasNin": False}}))
        got = self.get(f"families/{family.id}/payer-identity/", who=self.maker)
        self.assertEqual(got.json(), {"identity": {"hasBvn": True, "hasNin": False}})
        for response in (put, got, self.get("dashboard/"), self.get("batches/")):
            self.assertNotIn(self.BVN, response.content.decode())
        bad = self.put(f"families/{family.id}/payer-identity/", {"bvn": "12"}, who=self.maker)
        self.assertEqual(bad.json()["code"], "invalid_identity")
        self.assertEqual(self.put(f"families/{family.id}/payer-identity/", {"bvn": self.BVN}, who=self.checker).status_code, 403)

    def test_no_response_anywhere_carries_a_provider_credential(self):
        self.make_family("Alpha")
        batch = self.create_batch()
        self.post(self.batch_path(batch, "submit/"), {"expectedHash": batch["snapshotHash"]}, who=self.maker)
        for tail in ("dashboard/", "batches/", self.batch_path(batch), self.batch_path(batch, "items/"), self.batch_path(batch, "events/"), "policy/", "switches/"):
            self.assertNotIn("sandbox-key-1", json.dumps(self.get(tail).json()), tail)
