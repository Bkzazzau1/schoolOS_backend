from apps.notifications.models import Notification
from apps.staff.tests.helpers import StaffTestCase
from apps.sync.models import SyncRecord

TYPE = "concession_request"


def request(id="CNC-2026-050", **over):
    body = {"id": id, "student": "Yusuf Bello", "className": "JSS 2B", "type": "scholarship", "grossFee": 185000,
            "amount": 75000, "reason": "Founder Scholarship", "requestedBy": "Finance Office", "requestedByRole": "Owner",
            "requestedAt": "01 Jan 2020", "status": "pendingApproval", "decidedBy": None, "decidedAt": None,
            "decisionNote": None}
    body.update(over)
    return body


class ConcessionTestCase(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.finance = self.members["accountant"]

    def submit(self, who=None, id="CNC-2026-050", **over):
        return self.push(TYPE, id, request(id, **over), who=who or self.finance)

    def decide(self, status, who=None, id="CNC-2026-050", **over):
        stored = SyncRecord.objects.get(school=self.school, entity_type=TYPE, entity_id=id).payload
        return self.push(TYPE, id, {**stored, "status": status, **over}, operation="update", who=who or self.owner)

    def stored_request(self, id="CNC-2026-050"):
        return self.stored(TYPE, id).payload

    def give_duty(self, member, duty="finance.concessions", status="active"):
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id=f"JOB-{member.id}",
            payload={"registeredStaffId": f"S-{member.id}", "duties": [duty], "status": status,
                     "membershipId": str(member.id)},
        )


class SubmittingTests(ConcessionTestCase):
    def test_finance_asks_and_the_server_records_who_and_when(self):
        self.ok(self.submit())
        p = self.stored_request()
        self.assertEqual((p["status"], p["requestedByMembershipId"], p["requestedByRole"]),
                         ("pendingApproval", str(self.finance.id), "Finance Office"))
        self.assertNotEqual(p["requestedAt"], "01 Jan 2020")     # the date is the server's
        self.assertRegex(p["requestedAt"], r"^\d{2} [A-Z][a-z]{2} \d{4}$")
        self.assertTrue(p["createdAt"])

    def test_the_owner_may_ask_too(self):
        self.ok(self.submit(who=self.owner))
        self.assertEqual(self.stored_request()["requestedByRole"], "Proprietor")

    def test_others_cannot_ask_unless_the_owner_gave_them_the_duty(self):
        for role in ("principal", "administrator", "teacher", "staff", "parent", "student"):
            self.rejected(self.submit(who=self.members[role]), "ask for a concession")
        person = self.members["administrator"]
        self.give_duty(person, status="pendingActivation")
        self.rejected(self.submit(who=person), "ask for a concession")
        self.give_duty(self.members["principal"], duty="finance.fees")          # a different duty
        self.rejected(self.submit(who=self.members["principal"]), "ask for a concession")
        SyncRecord.objects.filter(entity_id=f"JOB-{person.id}").update(
            payload={"duties": ["finance.concessions"], "status": "active", "membershipId": str(person.id)})
        self.ok(self.submit(who=person))

    def test_a_new_request_must_start_pending_and_carry_no_decision(self):
        for status in ("approved", "declined", "paid"):
            self.rejected(self.submit(status=status), "starts as pending")
        self.ok(self.submit(decidedBy="Proprietor", decidedAt="02 Sep 2026", decisionNote="done", requestedByMembershipId="fake"))
        p = self.stored_request()
        self.assertEqual((p["decidedBy"], p["decidedAt"], p["decisionNote"], p["requestedByMembershipId"]),
                         (None, None, None, str(self.finance.id)))

    def test_the_amounts_must_make_sense(self):
        for over in ({"amount": 0}, {"amount": -5}, {"amount": 185001}, {"grossFee": -1}, {"amount": "75000"},
                     {"amount": 75000.5}, {"amount": True}, {"grossFee": 10**9}):
            self.rejected(self.submit(**over))
        self.assertFalse(SyncRecord.objects.filter(entity_type=TYPE).exists())

    def test_the_other_fields_are_checked(self):
        for over in ({"student": ""}, {"className": ""}, {"requestedBy": ""}, {"type": "gift"}, {"reason": "x" * 301}):
            self.rejected(self.submit(**over))
        self.rejected(self.push(TYPE, "CNC-2026-050", request("CNC-2026-999"), who=self.finance), "id must match")
        self.rejected(self.submit(id="x"), "not valid")

    def test_a_missing_reason_gets_a_plain_one(self):
        self.ok(self.submit(reason="", type="discount"))
        self.assertEqual(self.stored_request()["reason"], "Discount request")

    def test_two_devices_using_the_same_number_are_told_about_the_conflict(self):
        self.ok(self.submit())
        self.assertEqual(self.submit(student="Someone Else").status_code, 409)
        self.assertEqual(self.stored_request()["student"], "Yusuf Bello")

    def test_it_cannot_be_deleted_or_used_across_schools(self):
        self.ok(self.submit())
        self.rejected(self.push(TYPE, "CNC-2026-050", operation="delete", who=self.owner), "cannot be deleted")
        self.assertEqual(self.push(TYPE, "CNC-2026-051", request("CNC-2026-051"), who=self.other_owner, school=self.school).status_code, 403)


class DecidingTests(ConcessionTestCase):
    def setUp(self):
        super().setUp()
        self.ok(self.submit())

    def test_the_owner_approves_and_it_is_recorded(self):
        self.ok(self.decide("approved", decisionNote="Per fund allocation"))
        p = self.stored_request()
        self.assertEqual((p["status"], p["decidedBy"], p["decidedByMembershipId"], p["decisionNote"]),
                         ("approved", "Proprietor", str(self.owner.id), "Per fund allocation"))
        self.assertRegex(p["decidedAt"], r"^\d{2} [A-Z][a-z]{2} \d{4}$")

    def test_an_approval_needs_no_note_but_a_decline_does(self):
        self.ok(self.decide("approved"))
        self.ok(self.submit(id="CNC-2026-051"))
        self.rejected(self.decide("declined", id="CNC-2026-051"), "decisionNote")
        self.ok(self.decide("declined", id="CNC-2026-051", decisionNote="Not eligible"))
        self.assertEqual(self.stored_request("CNC-2026-051")["status"], "declined")

    def test_nobody_but_the_owner_or_someone_given_billing_authority_decides(self):
        # Asking for concessions (finance.concessions) and other finance duties do not carry the power to decide.
        self.give_duty(self.members["administrator"])
        self.give_duty(self.members["principal"], duty="finance.approvals")
        for who in (self.finance, self.members["principal"], self.members["administrator"], self.members["teacher"]):
            self.rejected(self.decide("approved", who=who), "can decide a concession")
        self.assertEqual(self.stored_request()["status"], "pendingApproval")

    def test_a_decision_is_final(self):
        self.ok(self.decide("approved"))
        for status in ("approved", "declined", "pendingApproval"):
            self.rejected(self.decide(status, decisionNote="x"), "already approved")

    def test_the_amounts_cannot_be_changed_while_deciding(self):
        self.ok(self.decide("approved", amount=185000, student="Someone Else", type="discount", grossFee=1,
                            requestedByMembershipId=str(self.owner.id), requestedBy="Forged"))
        p = self.stored_request()
        self.assertEqual((p["amount"], p["student"], p["type"], p["grossFee"], p["requestedBy"], p["requestedByMembershipId"]),
                         (75000, "Yusuf Bello", "scholarship", 185000, "Finance Office", str(self.finance.id)))

    def test_only_approve_or_decline_are_decisions(self):
        for status in ("paid", "", None):
            self.rejected(self.decide(status))

    def test_a_stale_device_gets_a_conflict(self):
        stored = self.stored_request()
        response = self.push(TYPE, "CNC-2026-050", {**stored, "status": "approved"}, operation="update", base_version=7)
        self.assertEqual(response.status_code, 409)


class TellingPeopleTests(ConcessionTestCase):
    def told(self, who, kind):
        return list(Notification.objects.filter(recipient=who, kind=kind).order_by("created_at", "id").values_list("message", flat=True))

    def test_the_owner_hears_of_a_request_and_finance_hears_the_answer(self):
        self.ok(self.submit())
        (message,) = self.told(self.owner, "concession_request")
        self.assertIn("Yusuf Bello", message)
        self.assertIn("₦75,000", message)
        self.ok(self.decide("declined", decisionNote="Not eligible"))
        (answer,) = self.told(self.finance, "concession_decided")
        self.assertIn("declined", answer)
        self.assertIn("Not eligible", answer)

    def test_nobody_is_told_about_their_own_action(self):
        self.ok(self.submit(who=self.owner))
        self.assertEqual(self.told(self.owner, "concession_request"), [])
        self.ok(self.decide("approved"))
        self.assertEqual(self.told(self.owner, "concession_decided"), [])


class WhoReceivesItTests(ConcessionTestCase):
    def kinds(self, who):
        self.client.force_authenticate(who.user)
        found = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id)}).json()["records"]
        return {r["entityType"] for r in found}

    def test_only_finance_and_the_owner_receive_requests(self):
        self.ok(self.submit())
        for who in (self.owner, self.finance):
            self.assertIn(TYPE, self.kinds(who), who.role)
        for role in ("principal", "administrator", "teacher", "staff", "parent", "student"):
            self.assertNotIn(TYPE, self.kinds(self.members[role]), role)

    def test_someone_with_the_duty_receives_them_and_the_person_who_asked_keeps_theirs(self):
        person = self.members["administrator"]
        self.give_duty(person)
        self.ok(self.submit(who=person))
        self.assertIn(TYPE, self.kinds(person))
        SyncRecord.objects.filter(entity_type="owner_job_assignment").delete()     # the duty is withdrawn
        self.assertIn(TYPE, self.kinds(person))                                   # still sees what they asked for
        self.assertNotIn(TYPE, self.kinds(self.members["principal"]))
