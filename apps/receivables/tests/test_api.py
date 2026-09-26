import json
from datetime import timedelta

from django.contrib.auth import get_user_model

from apps.schools.models import Membership, Role
from apps.students.models import GuardianLink

from .. import adjustments, allocation, collection_accounts, families
from ..models import FamilyCollectionAccount, ReceivableAdjustment, StudentReceivable
from .base import ReceivablesTestCase, CLOCK

N = 100
User = get_user_model()


class ApiTestCase(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.authority = self.members["principal"]
        self.give_duty(self.authority)
        self.finance = self.members["accountant"]

    def url(self, tail, school=None):
        return f"/api/v1/schools/{(school or self.school).id}/receivables/{tail}"

    def get(self, tail, who=None, school=None):
        self.client.force_authenticate((who or self.owner).user)
        return self.client.get(self.url(tail, school))

    def post(self, tail, data=None, who=None, school=None):
        self.client.force_authenticate((who or self.owner).user)
        return self.client.post(self.url(tail, school), data or {}, format="json")

    def with_fees(self):
        self.bello_family()
        self.schedule = self.publish_bello_fees()
        self.tuition = self.charge(self.ahmad)


class WhoMayReadAndWhoMayDecideTests(ApiTestCase):
    READS = ("families/", "fee-schedules/", "charges/", "adjustments/", "families/unassigned-students/", "families/bridge/")

    def test_the_owner_the_finance_office_and_a_delegate_may_read(self):
        for who in (self.owner, self.finance, self.authority):
            for tail in self.READS:
                self.assertEqual(self.get(tail, who=who).status_code, 200, (who.role, tail))

    def test_nobody_else_may_read(self):
        for role in ("administrator", "teacher", "staff", "parent", "student", "driver", "alumni"):
            for tail in self.READS:
                self.assertEqual(self.get(tail, who=self.members[role]).status_code, 403, (role, tail))

    def test_only_a_billing_authority_may_change_what_families_owe_not_even_the_finance_office(self):
        self.with_fees()
        sid = str(self.schedule.id)
        cid = str(self.tuition.id)
        session = str(self.session.id)
        attempts = [
            ("fee-schedules/", {"sessionId": session, "name": "X"}),
            (f"fee-schedules/{sid}/publish/", {}), (f"fee-schedules/{sid}/retire/", {"reason": "x"}),
            (f"fee-schedules/{sid}/clone/", {}), (f"fee-schedules/{sid}/void-charges/", {"reason": "x"}),
            (f"fee-schedules/{sid}/items/", {"code": "X", "name": "X", "amountMinor": 1, "dueDate": "2026-10-01"}),
            (f"charges/{cid}/adjust/", {"kind": "discount", "amountMinor": 100, "reason": "x"}),
            (f"charges/{cid}/void/", {"reason": "x"}),
        ]
        for tail, data in attempts:
            self.assertEqual(self.post(tail, data, who=self.finance).status_code, 403, tail)
            self.assertEqual(self.post(tail, data, who=self.members["teacher"]).status_code, 403, tail)
        self.assertFalse(ReceivableAdjustment.objects.exists())
        self.assertEqual(self.post(f"charges/{cid}/adjust/", {"kind": "discount", "amountMinor": 100, "reason": "x"}, who=self.authority).status_code, 201)

    def test_another_schools_owner_is_shut_out_and_our_objects_look_like_they_do_not_exist(self):
        self.with_fees()
        for tail in (f"families/{self.family.id}/", f"fee-schedules/{self.schedule.id}/", f"charges/{self.tuition.id}/",
                     f"families/{self.family.id}/statement/", f"families/{self.family.id}/credit/", f"families/{self.family.id}/payments/"):
            self.assertEqual(self.get(tail, who=self.other_owner).status_code, 403, tail)  # naming our school
            self.assertEqual(self.get(tail, who=self.other_owner, school=self.other_school).status_code, 404, tail)  # naming theirs
        for tail in (f"families/{self.family.id}/rename/", f"charges/{self.tuition.id}/adjust/", f"fee-schedules/{self.schedule.id}/publish/"):
            self.assertEqual(self.post(tail, {"displayName": "x", "kind": "discount", "amountMinor": 1, "reason": "x"}, who=self.other_owner, school=self.other_school).status_code, 404, tail)
        self.assertEqual(self.tuition.gross_amount_minor, StudentReceivable.objects.get(pk=self.tuition.pk).gross_amount_minor)


class FamilyApiTests(ApiTestCase):
    def test_a_family_is_made_with_its_students_and_their_guardians_become_payers(self):
        a = self.make_student("Ahmad", "Bello", guardian="Musa Bello", phone="08031111111")
        b = self.make_student("Aisha", "Bello", guardian="Musa Bello", phone="08031111111")
        response = self.post("families/", {"displayName": "The Bellos", "studentIds": [str(a.id), str(b.id)]}, who=self.finance)
        self.assertEqual(response.status_code, 201, response.json())
        family = response.json()["family"]
        self.assertRegex(family["code"], r"^FAM-")
        self.assertEqual({s["name"] for s in family["students"]}, {"Ahmad Bello", "Aisha Bello"})
        self.assertEqual([(g["name"], g["isPrimaryPayer"]) for g in family["guardians"] if g["isPrimaryPayer"]], [("Musa Bello", True)])
        self.assertEqual(family["position"]["outstandingMinor"], 0)

    def test_the_rules_are_enforced_through_the_api_too(self):
        a = self.make_student("Ahmad", "Bello")
        self.post("families/", {"displayName": "First", "studentIds": [str(a.id)]})
        clash = self.post("families/", {"displayName": "Second", "studentIds": [str(a.id)]})
        self.assertEqual((clash.status_code, clash.json()["code"]), (400, "student_in_family"))
        for data, code in (({"displayName": ""}, "family_name_required"), ({"displayName": "X", "studentIds": "nope"}, "invalid_students"),
                           ({"displayName": "X", "studentIds": ["not-a-uuid"]}, "invalid_reference"),
                           ({"displayName": "X", "studentIds": ["00000000-0000-0000-0000-000000000000"]}, "student_not_found")):
            response = self.post("families/", data)
            self.assertEqual((response.status_code, response.json()["code"]), (400, code), data)
        stranger = self.make_student("Zed", "Other", school=self.other_school)
        self.assertEqual(self.post("families/", {"displayName": "X", "studentIds": [str(stranger.id)]}).json()["code"], "student_not_found")

    def test_list_search_rename_add_remove_link_and_close(self):
        a, b = self.make_student("Ahmad", "Bello", guardian="Musa"), self.make_student("Aisha", "Bello", guardian="Musa")
        family = self.post("families/", {"displayName": "The Bellos", "studentIds": [str(a.id)]}).json()["family"]
        fid = family["id"]
        self.assertEqual([f["id"] for f in self.get("families/?q=bello").json()["families"]], [fid])
        self.assertEqual(self.get("families/?q=zzz").json()["families"], [])
        self.assertEqual(self.post(f"families/{fid}/rename/", {"displayName": "Bello household"}).json()["family"]["displayName"], "Bello household")
        added = self.post(f"families/{fid}/add-student/", {"studentId": str(b.id)}).json()["family"]
        self.assertEqual(len(added["students"]), 2)
        guardian = b.guardians.get()
        linked = self.post(f"families/{fid}/link-guardian/", {"guardianId": str(guardian.id), "primary": True}).json()["family"]
        self.assertTrue(any(g["isPrimaryPayer"] for g in linked["guardians"]))
        removed = self.post(f"families/{fid}/remove-student/", {"studentId": str(b.id)}).json()["family"]
        self.assertEqual(len(removed["students"]), 1)
        self.assertEqual(self.post(f"families/{fid}/remove-student/", {"studentId": str(b.id)}).json()["code"], "not_a_member")
        self.assertEqual(self.post(f"families/{fid}/set-status/", {"status": "inactive"}).json()["family"]["status"], "inactive")
        self.assertEqual(self.post(f"families/{fid}/set-status/", {"status": "gone"}).json()["code"], "invalid_status")

    def test_students_with_no_family_are_listed_and_the_bridge_reports_before_it_acts(self):
        self.make_student("Ahmad", "Bello", guardian="Musa", ref="Bello Family")
        self.make_student("Lone", "Wolf")
        listed = self.get("families/unassigned-students/").json()["students"]
        self.assertEqual(len(listed), 2)
        report = self.get("families/bridge/").json()["report"]
        self.assertEqual((len(report["groups"]), len(report["withoutReference"])), (1, 1))
        self.assertEqual(self.get("families/").json()["families"], [])  # a report changes nothing
        done = self.post("families/bridge/", {"singletons": True}).json()["report"]
        self.assertEqual((done["familiesCreated"], done["studentsLinked"]), (2, 2))
        self.assertEqual(self.get("families/unassigned-students/").json()["students"], [])

    def test_the_family_detail_never_shows_provider_internals(self):
        self.with_fees()
        collection_accounts.register(self.family, provider="monnify", account_number="8012345678", actor=self.owner, provider_meta={"apiKey": "SECRET-KEY"})
        text = json.dumps(self.get(f"families/{self.family.id}/").json())
        self.assertIn("8012345678", text)
        self.assertNotIn("SECRET-KEY", text)
        self.assertNotIn("providerMeta", text)


class FeeScheduleApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.bello_family()

    def test_the_whole_lifecycle_through_the_api(self):
        created = self.post("fee-schedules/", {"sessionId": str(self.session.id), "termId": str(self.term.id), "name": "First Term"}, who=self.authority)
        self.assertEqual(created.status_code, 201, created.json())
        sid = created.json()["schedule"]["id"]
        due = (CLOCK + timedelta(days=10)).isoformat()
        item = self.post(f"fee-schedules/{sid}/items/", {"code": "TUI", "name": "Tuition", "category": "tuition", "amountMinor": 12_000_000, "dueDate": due, "scope": "student", "studentId": str(self.ahmad.id)}, who=self.authority)
        self.assertEqual(item.status_code, 201, item.json())
        iid = item.json()["item"]["id"]
        self.assertEqual(item.json()["item"]["amountMinor"], 12_000_000)
        changed = self.post(f"fee-schedules/{sid}/items/{iid}/update/", {"amountMinor": 13_000_000}, who=self.authority)
        self.assertEqual(changed.json()["item"]["amountMinor"], 13_000_000)
        preview = self.get(f"fee-schedules/{sid}/preview/").json()["preview"]
        self.assertEqual((preview["totalMinor"], preview["items"][0]["chargeable"], preview["problems"]), (13_000_000, 1, []))
        published = self.post(f"fee-schedules/{sid}/publish/", who=self.authority)
        self.assertEqual(published.status_code, 200, published.json())
        report = published.json()["report"]
        self.assertEqual((report["created"], report["families"], report["complete"], published.json()["schedule"]["status"]), (1, 1, True, "published"))
        self.assertEqual(self.post(f"fee-schedules/{sid}/publish/", who=self.authority).json()["code"], "already_published")
        again = self.post(f"fee-schedules/{sid}/refresh/", who=self.authority).json()["report"]
        self.assertEqual((again["created"], again["alreadyExisted"]), (0, 1))
        frozen = self.post(f"fee-schedules/{sid}/items/{iid}/update/", {"amountMinor": 1}, who=self.authority)
        self.assertEqual((frozen.status_code, frozen.json()["code"]), (400, "schedule_not_draft"))
        cloned = self.post(f"fee-schedules/{sid}/clone/", who=self.authority)
        self.assertEqual((cloned.status_code, cloned.json()["schedule"]["status"], len(cloned.json()["schedule"]["items"])), (201, "draft", 1))
        retired = self.post(f"fee-schedules/{sid}/retire/", {"reason": "Wrong term"}, who=self.authority)
        self.assertEqual(retired.json()["schedule"]["status"], "retired")
        self.assertEqual(len(self.get("fee-schedules/?status=retired").json()["schedules"]), 1)

    def test_bad_input_is_refused_with_a_reason_and_stores_nothing(self):
        sid = self.post("fee-schedules/", {"sessionId": str(self.session.id), "name": "X"}).json()["schedule"]["id"]
        cases = [
            ({"code": "A", "name": "A", "amountMinor": 0}, "invalid_amount"), ({"code": "A", "name": "A", "amountMinor": 10.5}, "invalid_amount"),
            ({"code": "A", "name": "A", "amountMinor": 100, "scope": "class"}, "invalid_class"),
            ({"code": "A", "name": "A", "amountMinor": 100, "bogus": 1}, "unexpected_field"),
            ({"code": "A", "name": "A", "amountMinor": 100, "studentId": "not-a-uuid", "scope": "student"}, "invalid_reference"),
            ({"code": "A", "name": "A", "amountMinor": 100, "plan": [{"basisPoints": 5000, "dueDate": "2026-09-30"}]}, "invalid_plan"),
        ]
        for data, code in cases:
            response = self.post(f"fee-schedules/{sid}/items/", data)
            self.assertEqual((response.status_code, response.json()["code"]), (400, code), data)
        self.assertEqual(self.get(f"fee-schedules/{sid}/").json()["schedule"]["items"], [])
        self.assertEqual(self.post("fee-schedules/", {"sessionId": "nope", "name": "X"}).json()["code"], "invalid_reference")

    def test_the_preview_lists_who_would_not_be_charged_and_why(self):
        orphan = self.make_student("Ola", "Alone")
        self.enroll(orphan, self.term.session.school.academic_classes.first(), self.session)
        sid = self.post("fee-schedules/", {"sessionId": str(self.session.id), "name": "All"}).json()["schedule"]["id"]
        self.post(f"fee-schedules/{sid}/items/", {"code": "TUI", "name": "Tuition", "amountMinor": 100_000, "dueDate": "2026-10-30"})
        preview = self.get(f"fee-schedules/{sid}/preview/").json()["preview"]
        self.assertEqual([s["id"] for s in preview["withoutFamily"]], [str(orphan.id)])
        self.assertEqual(preview["items"][0]["students"], preview["items"][0]["chargeable"] + 1)

    def test_a_class_and_a_section_can_be_aimed_at(self):
        klass = self.term.session.school.academic_classes.get(code="PRI3")
        sid = self.post("fee-schedules/", {"sessionId": str(self.session.id), "name": "Aimed"}).json()["schedule"]["id"]
        due = "2026-10-30"
        self.assertEqual(self.post(f"fee-schedules/{sid}/items/", {"code": "C", "name": "Class", "amountMinor": 500, "dueDate": due, "scope": "class", "academicClassId": str(klass.id)}).status_code, 201)
        self.assertEqual(self.post(f"fee-schedules/{sid}/items/", {"code": "S", "name": "Section", "amountMinor": 500, "dueDate": due, "scope": "section", "section": "Secondary"}).status_code, 201)
        stranger_class = self.make_year(self.other_school)[2]
        bad = self.post(f"fee-schedules/{sid}/items/", {"code": "X", "name": "Foreign", "amountMinor": 500, "dueDate": due, "scope": "class", "academicClassId": str(stranger_class.id)})
        self.assertEqual((bad.status_code, bad.json()["code"] if bad.status_code == 400 else "404"), (404, "404"))  # not found at this school


class ChargesAndAdjustmentsApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.with_fees()

    def test_charges_are_listed_filtered_and_paged_with_money_in_kobo(self):
        listed = self.get("charges/").json()
        self.assertEqual((listed["total"], listed["hasMore"], len(listed["receivables"])), (3, False, 3))
        first = listed["receivables"][0]
        for key in ("grossMinor", "adjustmentsMinor", "netMinor", "paidMinor", "outstandingMinor"):
            self.assertIsInstance(first[key], int)
        self.assertEqual(self.get(f"charges/?student={self.ahmad.id}").json()["total"], 1)
        self.assertEqual(self.get(f"charges/?family={self.family.id}&status=open").json()["total"], 3)
        self.assertEqual(self.get("charges/?status=settled").json()["total"], 0)
        self.assertEqual(len(self.get("charges/?limit=2").json()["receivables"]), 2)
        self.assertTrue(self.get("charges/?limit=2").json()["hasMore"])
        self.assertEqual(self.get("charges/?limit=lots").status_code, 400)
        self.assertEqual(self.get("charges/?student=nope").status_code, 400)

    def test_an_adjustment_never_changes_the_gross_and_shows_in_the_detail_and_the_history(self):
        made = self.post(f"charges/{self.tuition.id}/adjust/", {"kind": "scholarship", "amountMinor": 2_000_000, "reason": "Founder scholarship"}, who=self.authority)
        self.assertEqual(made.status_code, 201, made.json())
        charge = made.json()["receivable"]
        self.assertEqual((charge["grossMinor"], charge["adjustmentsMinor"], charge["netMinor"]), (12_000_000, 2_000_000, 10_000_000))
        detail = self.get(f"charges/{self.tuition.id}/").json()["receivable"]
        self.assertEqual([a["reason"] for a in detail["adjustments"]], ["Founder scholarship"])
        history = self.get(f"adjustments/?receivable={self.tuition.id}").json()
        self.assertEqual((history["total"], history["adjustments"][0]["kind"]), (1, "scholarship"))
        self.assertEqual(history["adjustments"][0]["authorizedBy"], str(self.authority.id))

    def test_bad_adjustments_are_refused_with_a_reason(self):
        for data, code in (({"kind": "scholarship", "amountMinor": 0, "reason": "x"}, "invalid_amount"), ({"kind": "gift", "amountMinor": 5, "reason": "x"}, "invalid_kind"),
                           ({"kind": "waiver", "amountMinor": 5, "reason": ""}, "reason_required"), ({"kind": "waiver", "amountMinor": 12_000_001, "reason": "x"}, "exceeds_charge")):
            response = self.post(f"charges/{self.tuition.id}/adjust/", data)
            self.assertEqual((response.status_code, response.json()["code"]), (400, code), data)
        self.assertFalse(ReceivableAdjustment.objects.exists())

    def test_reverse_and_void_through_the_api(self):
        adjustment = self.post(f"charges/{self.tuition.id}/adjust/", {"kind": "discount", "amountMinor": 1_000_000, "reason": "x"}).json()["adjustments"][0]
        reversal = self.post(f"adjustments/{adjustment['id']}/reverse/", {"reason": "Mistake"})
        self.assertEqual((reversal.status_code, reversal.json()["adjustment"]["isReversal"]), (201, True))
        self.assertEqual(self.post(f"adjustments/{adjustment['id']}/reverse/", {"reason": "Again"}).json()["code"], "already_reversed")
        voided = self.post(f"charges/{self.tuition.id}/void/", {"reason": "Raised in error"})
        self.assertEqual(voided.json()["receivable"]["status"], "void")
        self.assertEqual(self.post(f"charges/{self.tuition.id}/void/", {"reason": "x"}).json()["code"], "receivable_void")

    def test_void_charges_of_a_schedule_skips_the_paid_ones_unless_told_not_to(self):
        allocation.allocate(self.payment(120_000 * N), self.family, prefer_student=self.ahmad)
        result = self.post(f"fee-schedules/{self.schedule.id}/void-charges/", {"reason": "Wrong fees"}).json()
        self.assertEqual((result["voided"], result["skippedPaid"]), (2, [str(self.tuition.id)]))


class StatementCreditAndPaymentApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.with_fees()

    def test_the_statement_is_derived_and_issuing_numbers_it(self):
        adjustments.adjust(self.tuition, kind="scholarship", amount_minor=20_000 * N, reason="x", actor=self.owner)
        s = self.get(f"families/{self.family.id}/statement/?session={self.session.id}").json()["statement"]
        self.assertEqual((s["totals"]["netMinor"], s["position"]["outstandingMinor"]), (280_000 * N, 280_000 * N))
        issued = self.post(f"families/{self.family.id}/statements/", {"sessionId": str(self.session.id), "termId": str(self.term.id)}, who=self.finance)
        self.assertEqual(issued.status_code, 201)
        self.assertRegex(issued.json()["statement"]["number"], r"^STM-\d{4}-000001$")
        self.assertEqual(len(self.get(f"families/{self.family.id}/statements/").json()["statements"]), 1)
        self.assertEqual(self.get(f"families/{self.family.id}/statement/?session=nope").status_code, 400)

    def test_credit_and_refunds(self):
        allocation.allocate(self.payment(400_000 * N), self.family)
        credit_view = self.get(f"families/{self.family.id}/credit/").json()
        self.assertEqual((credit_view["creditMinor"], credit_view["entries"][0]["kind"]), (100_000 * N, "overpayment"))
        refunded = self.post(f"families/{self.family.id}/credit/refund/", {"amountMinor": 30_000 * N, "reason": "Paid back"}, who=self.finance)
        self.assertEqual((refunded.status_code, refunded.json()["creditMinor"]), (201, 70_000 * N))
        self.assertEqual(self.post(f"families/{self.family.id}/credit/refund/", {"amountMinor": 999_999 * N, "reason": "x"}).json()["code"], "insufficient_credit")
        self.assertEqual(self.post(f"families/{self.family.id}/credit/refund/", {"amountMinor": 5, "reason": "x"}, who=self.members["teacher"]).status_code, 403)

    def test_a_families_payments_and_the_finance_office_correcting_one(self):
        tx = self.payment(120_000 * N)
        allocation.allocate(tx, self.family, prefer_student=self.ahmad)
        payments = self.get(f"families/{self.family.id}/payments/").json()["payments"]
        self.assertEqual((len(payments), payments[0]["amountMinor"], payments[0]["allocations"][0]["receivableId"]), (1, 120_000 * N, str(self.tuition.id)))
        aisha = self.charge(self.aisha)
        moved = self.post(f"payments/{tx.id}/reallocate/", {"reason": "Parent said it was for Aisha", "plan": [{"receivableId": str(aisha.id), "amountMinor": 100_000 * N}]}, who=self.finance)
        self.assertEqual((moved.status_code, moved.json()["allocatedMinor"], moved.json()["creditMinor"]), (200, 100_000 * N, 20_000 * N))
        self.assertEqual(self.post(f"payments/{tx.id}/reallocate/", {"reason": "x", "plan": []}, who=self.finance).json()["code"], "empty_plan")
        self.assertEqual(self.post(f"payments/{tx.id}/reallocate/", {"reason": "x", "plan": [{"receivableId": str(aisha.id), "amountMinor": 5}]}, who=self.members["teacher"]).status_code, 403)
        self.assertEqual(self.post(f"payments/{tx.id}/reallocate/", {"reason": "x", "plan": ["nonsense"]}, who=self.finance).json()["code"], "invalid_plan")
        self.assertEqual(self.post(f"payments/{tx.id}/reallocate/", {"reason": "x", "plan": [{"receivableId": str(aisha.id), "amountMinor": 5}]}, who=self.other_owner, school=self.other_school).status_code, 404)
        self.assertLedgerHolds()


class CollectionAccountApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.with_fees()

    def test_register_then_suspend_reinstate_and_close(self):
        made = self.post(f"families/{self.family.id}/collection-accounts/legacy/", {"provider": "monnify", "accountNumber": "8012345678", "accountName": "BRIGHTGATE / BELLO", "bankName": "Wema"})
        self.assertEqual((made.status_code, made.json()["account"]["status"], made.json()["account"]["origin"]), (201, "active", "legacy_manual"))
        aid = made.json()["account"]["id"]
        self.assertEqual(self.post(f"collection-accounts/{aid}/suspend/", {"reason": "Provider flagged it"}, who=self.finance).json()["account"]["status"], "suspended")
        self.assertEqual(self.post(f"collection-accounts/{aid}/reinstate/", who=self.finance).json()["account"]["status"], "active")
        self.assertEqual(self.post(f"collection-accounts/{aid}/close/", {"reason": "Moved provider"}, who=self.finance).json()["account"]["status"], "closed")
        self.assertEqual(self.post(f"collection-accounts/{aid}/close/", {"reason": "Again"}, who=self.finance).json()["code"], "already_closed")

    def test_recording_by_hand_is_for_provider_managers_only_and_only_their_own_school(self):
        data = {"provider": "monnify", "accountNumber": "8012345678"}
        for who in (self.members["teacher"], self.finance):
            self.assertEqual(self.post(f"families/{self.family.id}/collection-accounts/legacy/", data, who=who).status_code, 403, who.role)
        made = self.post(f"families/{self.family.id}/collection-accounts/legacy/", data).json()["account"]
        self.assertEqual(self.post(f"collection-accounts/{made['id']}/suspend/", {"reason": "x"}, who=self.other_owner, school=self.other_school).status_code, 404)
        stranger_connection = self.bank_connection(self.other_school)
        self.assertEqual(self.post(f"families/{self.family.id}/collection-accounts/legacy/", {"provider": "paystack", "accountNumber": "12345", "connectionId": str(stranger_connection.id)}).status_code, 404)

    def test_the_status_follows_what_the_family_owes_as_seen_through_the_api(self):
        made = self.post(f"families/{self.family.id}/collection-accounts/legacy/", {"provider": "monnify", "accountNumber": "8012345678"}).json()["account"]
        allocation.allocate(self.payment(300_000 * N), self.family)
        listed = self.get(f"families/{self.family.id}/collection-accounts/").json()["accounts"]
        self.assertEqual((listed[0]["id"], listed[0]["status"]), (made["id"], "dormant"))
        self.assertEqual(FamilyCollectionAccount.objects.count(), 1)


class ParentApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.with_fees()
        self.parent = self.members["parent"]
        GuardianLink.objects.filter(student__in=[self.ahmad, self.aisha, self.maryam]).update(account_user=self.parent.user)
        collection_accounts.register(self.family, provider="monnify", account_number="8012345678", account_name="BRIGHTGATE / BELLO", actor=self.owner, provider_meta={"apiKey": "SECRET-KEY"})
        adjustments.adjust(self.tuition, kind="scholarship", amount_minor=20_000 * N, reason="Confidential reason", actor=self.owner)

    def test_a_parent_sees_their_own_family_what_is_owed_and_where_to_pay(self):
        response = self.get("me/families/", who=self.parent)
        self.assertEqual(response.status_code, 200)
        family = response.json()["families"][0]
        self.assertEqual((family["id"], family["position"]["outstandingMinor"]), (str(self.family.id), 280_000 * N))
        self.assertEqual(family["collectionAccounts"][0]["accountNumber"], "8012345678")
        statement = self.get(f"me/families/{self.family.id}/statement/", who=self.parent).json()["statement"]
        self.assertEqual(len(statement["students"]), 3)

    def test_a_parent_sees_nothing_of_how_the_school_decides_or_of_provider_internals(self):
        text = json.dumps([self.get("me/families/", who=self.parent).json(), self.get(f"me/families/{self.family.id}/statement/", who=self.parent).json()])
        for hidden in ("SECRET-KEY", "Confidential reason", "providerMeta", "authorizedBy", "requestedBy"):
            self.assertNotIn(hidden, text)

    def test_another_familys_parent_cannot_see_this_family(self):
        other_user = User.objects.create_user("other-parent@school.ng", "a-long-test-password-1")
        other = Membership.objects.create(user=other_user, school=self.school, role=Role.PARENT)
        stranger = self.make_student("Yusuf", "Sani", guardian="Ada Sani")
        GuardianLink.objects.filter(student=stranger).update(account_user=other_user)
        families.ensure_family_for_student(self.school, stranger)
        self.assertEqual(len(self.get("me/families/", who=other).json()["families"]), 1)
        self.assertNotEqual(self.get("me/families/", who=other).json()["families"][0]["id"], str(self.family.id))
        self.assertEqual(self.get(f"me/families/{self.family.id}/statement/", who=other).status_code, 404)

    def test_a_parent_with_no_linked_children_sees_nothing_and_staff_are_not_parents(self):
        unlinked = Membership.objects.create(user=User.objects.create_user("nobody@school.ng", "a-long-test-password-1"), school=self.school, role=Role.PARENT)
        self.assertEqual(self.get("me/families/", who=unlinked).json()["families"], [])
        for who in (self.owner, self.finance, self.members["teacher"], self.members["student"]):
            self.assertEqual(self.get("me/families/", who=who).status_code, 403, who.role)

    def test_a_parent_at_another_school_sees_nothing_here(self):
        self.assertEqual(self.get("me/families/", who=self.parent, school=self.other_school).status_code, 403)

    def test_a_parent_cannot_reach_any_finance_endpoint(self):
        for tail in ("families/", "charges/", f"families/{self.family.id}/", f"families/{self.family.id}/credit/"):
            self.assertEqual(self.get(tail, who=self.parent).status_code, 403, tail)
        self.assertEqual(self.post(f"charges/{self.tuition.id}/adjust/", {"kind": "waiver", "amountMinor": 1, "reason": "x"}, who=self.parent).status_code, 403)
