from datetime import timedelta

from django.utils import timezone

from apps.invitations.models import StaffInvitation
from apps.invitations.tests.helpers import InviteTestCase
from apps.sync.models import SyncRecord


class DashboardTestCase(InviteTestCase):
    def url(self, kind, school=None):
        return f"/api/v1/dashboards/schools/{(school or self.school).id}/{kind}/"

    def get(self, kind, who=None, school=None):
        self.client.force_authenticate(who.user if who else None)
        return self.client.get(self.url(kind, school))

    def owner_view(self):
        response = self.get("owner", self.owner)
        self.assertEqual(response.status_code, 200, response.json())
        return response.json()

    def make(self, entity_type, entity_id, payload):
        return SyncRecord.objects.create(school=self.school, entity_type=entity_type, entity_id=entity_id, payload=payload)

    def section(self, id, name, stage, classes=2):
        self.make("academic_section", id, {"id": id, "name": name, "stage": stage, "campus": "Kaduna", "classes": classes})

    def head(self, id, section_id, person):
        self.make("leadership_appointment", id, {"id": id, "person": person, "title": "Head", "level": "sectionHead",
                                                 "sectionId": section_id})

    def batch(self, period, status, total, lines=2):
        self.make("payroll_batch", period, {"period": period, "status": status, "total": total,
                                            "lines": [{"staffId": f"S{i}", "name": "n", "net": 1} for i in range(lines)]})

    def concession(self, id, status, amount, student="A Student", kind="scholarship"):
        self.make("concession_request", id, {"id": id, "status": status, "amount": amount, "student": student, "type": kind})


class AccessTests(DashboardTestCase):
    def test_the_owner_dashboard_is_the_owners_alone(self):
        self.assertEqual(self.get("owner", self.owner).status_code, 200)
        for role in ("principal", "administrator", "accountant", "teacher", "staff", "parent", "student"):
            self.assertEqual(self.get("owner", self.members[role]).status_code, 403, role)
        self.assertEqual(self.get("owner").status_code, 401)
        self.assertEqual(self.get("owner", self.other_owner).status_code, 403)

    def test_the_finance_dashboard_is_for_the_owner_and_finance_officers(self):
        for role in ("proprietor", "accountant"):
            self.assertEqual(self.get("finance", self.members[role]).status_code, 200, role)
        for role in ("principal", "administrator", "teacher", "staff", "parent", "student"):
            self.assertEqual(self.get("finance", self.members[role]).status_code, 403, role)
        self.assertEqual(self.get("finance", self.other_owner).status_code, 403)

    def test_it_says_what_it_cannot_show_yet(self):
        body = self.owner_view()
        for missing in ("students", "attendance", "fee collection"):
            self.assertIn(missing, body["notAvailableYet"])
        # Nothing on the dashboard pretends to know about students, attendance or fees.
        self.assertFalse({"students", "attendance", "feeCollection"} & set(body))

    def test_an_empty_school_is_quiet_and_does_not_fail(self):
        body = self.owner_view()
        self.assertEqual(body["attention"], [])
        self.assertEqual(body["staff"]["headcount"], 0)
        self.assertEqual(body["staff"]["monthlyPayroll"], {"gross": 0, "deductions": 0, "net": 0})
        self.assertEqual((body["payroll"]["batches"], body["payroll"]["latest"]), ([], None))
        self.assertEqual(body["structure"]["sections"], [])

    def test_a_schools_figures_never_include_another_schools_records(self):
        SyncRecord.objects.create(school=self.other_school, entity_type="payroll_batch", entity_id="2026-09",
                                  payload={"period": "2026-09", "status": "prepared", "total": 5, "lines": [{}]})
        body = self.owner_view()
        self.assertEqual(body["payroll"]["batches"], [])
        self.assertEqual(body["attention"], [])


class StaffFiguresTests(DashboardTestCase):
    def test_headcount_payroll_and_registration_come_from_the_real_records(self):
        a = self.make_staff(name="Aisha Bello", email="aisha@school.ng", gross=300000, deductions=30000, systemRole="teacher")
        self.make_staff(name="Musa Ibrahim", email="musa@school.ng", gross=200000, deductions=20000, systemRole="staff")
        staff = self.owner_view()["staff"]
        self.assertEqual(staff["headcount"], 2)
        self.assertEqual(staff["onPayroll"], 2)
        self.assertEqual(staff["monthlyPayroll"], {"gross": 500000, "deductions": 50000, "net": 450000})
        self.assertEqual(sum(staff["byCategory"].values()), 2)
        self.assertEqual(staff["registration"]["waitingForStaff"], 2)
        self.assertEqual(staff["withLogin"], 0)
        self.assertEqual(staff["filesComplete"] + staff["filesMissingDocuments"], 2)
        # A staff member taken off payroll leaves the payroll figures but not the headcount.
        record = SyncRecord.objects.get(entity_type="owner_payroll_profile", entity_id=a)
        record.payload = {**record.payload, "onPayroll": False}
        record.save()
        staff = self.owner_view()["staff"]
        self.assertEqual((staff["headcount"], staff["onPayroll"], staff["monthlyPayroll"]["net"]), (2, 1, 180000))

    def test_people_are_counted_by_section_and_with_a_login(self):
        a = self.make_staff(name="Aisha Bello", email="aisha@school.ng")
        self.link(a, self.members["teacher"])
        staff = self.owner_view()["staff"]
        self.assertEqual(staff["withLogin"], 1)
        self.assertEqual(sum(staff["bySection"].values()), 1)


class MoneyFiguresTests(DashboardTestCase):
    def test_payroll_batches_newest_first_with_what_is_waiting(self):
        self.batch("2026-07", "disbursementInstructed", 900000)
        self.batch("2026-08", "disbursementInstructed", 950000, lines=3)
        self.batch("2026-09", "prepared", 1000000)
        self.batch("2026-10", "approved", 1100000)
        self.batch("2026-11", "rejected", 1)
        pay = self.owner_view()["payroll"]
        self.assertEqual([b["period"] for b in pay["batches"]], ["2026-11", "2026-10", "2026-09", "2026-08", "2026-07"])
        self.assertEqual(pay["latest"]["period"], "2026-11")
        self.assertEqual([b["period"] for b in pay["waitingForApproval"]], ["2026-09"])
        self.assertEqual([b["period"] for b in pay["waitingForPayment"]], ["2026-10"])
        self.assertEqual(pay["instructedTotal"], 1850000)
        self.assertEqual(pay["batches"][3]["staff"], 3)

    def test_scholarships_and_discounts(self):
        self.concession("CNC-1", "pendingApproval", 10000)
        self.concession("CNC-2", "pendingApproval", 5000)
        self.concession("CNC-3", "approved", 75000, student="Yusuf Bello")
        self.concession("CNC-4", "approved", 10000, student=" yusuf bello ", kind="discount")
        self.concession("CNC-5", "approved", 50000, student="Muhammad Kabir")
        self.concession("CNC-6", "declined", 999)
        c = self.owner_view()["concessions"]
        self.assertEqual(c["pending"], {"count": 2, "amount": 15000})
        self.assertEqual((c["approved"]["count"], c["approved"]["amount"], c["approved"]["studentsSupported"]), (3, 135000, 2))
        self.assertEqual(c["approved"]["byType"], {"scholarship": 125000, "discount": 10000})
        self.assertEqual(c["declined"], {"count": 1})

    def test_finance_sees_payroll_and_concessions_and_the_same_numbers(self):
        self.batch("2026-09", "prepared", 1000000)
        self.concession("CNC-1", "approved", 75000)
        body = self.get("finance", self.members["accountant"]).json()
        self.assertEqual(body["payroll"], self.owner_view()["payroll"])
        self.assertEqual(body["concessions"]["approved"]["amount"], 75000)
        self.assertNotIn("attention", body)
        self.assertNotIn("staff", body)
        self.assertIn("fee collection", body["notAvailableYet"])


class StructureFiguresTests(DashboardTestCase):
    def test_sections_with_their_head_and_staff(self):
        self.section("primary", "Primary School", "Primary", classes=6)
        self.section("secondary", "Secondary School", "Secondary")
        self.head("L-1", "primary", "Mrs. Hauwa Sule")
        self.make_staff(name="Aisha Bello", email="a@school.ng", workArea="Primary")
        self.make_staff(name="Musa Ibrahim", email="m@school.ng", workArea="Primary School")
        self.make_staff(name="Sani Bello", email="s@school.ng", workArea="Secondary")
        self.make_staff(name="Lost One", email="l@school.ng", workArea="Transport")
        rows = {r["id"]: r for r in self.owner_view()["structure"]["sections"]}
        self.assertEqual((rows["primary"]["staff"], rows["primary"]["classes"]), (2, 6))
        self.assertEqual(rows["primary"]["head"], {"name": "Mrs. Hauwa Sule", "title": "Head"})
        self.assertEqual((rows["secondary"]["staff"], rows["secondary"]["head"]), (1, None))
        self.assertEqual(self.owner_view()["structure"]["staffInNoSection"], 1)


class AttentionTests(DashboardTestCase):
    def keys(self):
        return {i["key"]: i for i in self.owner_view()["attention"]}

    def test_each_kind_of_waiting_work_shows_up_with_its_count_and_screen(self):
        self.ok(self.propose(email="new@school.ng"))                                     # a proposal to decide
        self.batch("2026-09", "prepared", 1)
        self.batch("2026-08", "approved", 1)
        self.concession("CNC-1", "pendingApproval", 5)
        self.section("primary", "Primary", "Primary")                                     # no head
        self.make("owner_job_assignment", "J1", {"status": "pendingActivation"})
        self.make("owner_payroll_authorizer", "A1", {"status": "pendingActivation"})
        keys = self.keys()
        self.assertEqual({k: v["count"] for k, v in keys.items()},
                         {"staff_proposals": 1, "payroll_approval": 1, "payroll_payment": 1, "concessions": 1,
                          "sections_without_head": 1, "unclaimed_access": 2})
        self.assertEqual(keys["payroll_approval"]["screen"], "owner.payroll")
        self.assertEqual(keys["staff_proposals"]["severity"], "high")
        self.assertIn("1 staff proposal to decide", keys["staff_proposals"]["title"])

    def test_the_most_urgent_come_first(self):
        self.ok(self.propose(email="new@school.ng"))
        self.concession("CNC-1", "pendingApproval", 5)
        self.section("primary", "Primary", "Primary")
        severities = [i["severity"] for i in self.owner_view()["attention"]]
        self.assertEqual(severities, sorted(severities, key=["high", "medium", "info"].index))

    def test_finished_work_drops_off_the_list(self):
        self.ok(self.propose(email="new@school.ng"))
        self.assertIn("staff_proposals", self.keys())
        self.approve(self.owner, self.last_proposal_id)
        self.assertNotIn("staff_proposals", self.keys())

    def test_invitations_that_expired_or_never_arrived_are_flagged(self):
        staff_id, _ = self.staff_with_link(email="musa@school.ng")
        self.assertNotIn("invitations", self.keys())                  # sent and still valid
        invitation = StaffInvitation.objects.get(staff_id=staff_id, status="pending")
        invitation.expires_at = timezone.now() - timedelta(days=1)
        invitation.save()
        self.assertEqual(self.keys()["invitations"]["count"], 1)
        invitation.expires_at = timezone.now() + timedelta(days=1)
        invitation.sent_at = None
        invitation.save()
        self.assertEqual(self.keys()["invitations"]["count"], 1)      # never delivered

    def test_submitted_registrations_and_missing_documents_are_flagged(self):
        staff_id = self.make_staff(email="musa@school.ng")
        record = SyncRecord.objects.get(entity_type="owner_staff_profile", entity_id=staff_id)
        record.payload = {**record.payload, "onboardingStatus": "submitted"}
        record.save()
        directory = SyncRecord.objects.get(entity_type="administrator_staff_directory", entity_id=staff_id)
        directory.payload = {**directory.payload, "fileStatus": "Missing document"}
        directory.save()
        keys = self.keys()
        self.assertEqual((keys["registrations"]["count"], keys["missing_documents"]["count"]), (1, 1))


class ScreenKeyTests(DashboardTestCase):
    def test_every_screen_the_attention_list_points_to_is_a_real_activity(self):
        import re
        from pathlib import Path

        from apps.access.catalog import ACTIVITIES
        from apps.dashboards import attention

        screens = set(re.findall(r'"(owner\.[a-z-]+)"', Path(attention.__file__).read_text(encoding="utf8")))
        self.assertGreaterEqual(len(screens), 4)
        self.assertEqual(screens - set(ACTIVITIES), set())
