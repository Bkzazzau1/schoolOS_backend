from apps.sync.models import SyncRecord

from .helpers import OwnerTestCase


def job(**extra):
    payload = {
        "name": "Mrs. Hauwa Sule", "email": "", "title": "Head of Primary",
        "recipientType": "registered", "registeredStaffId": "STAFF-021",
        "role": "sectionHead", "sectionId": "SEC-PRIMARY", "sectionName": "Primary School",
        "duties": ["academics.teaching", "administration.students"],
        "status": "pendingActivation",
    }
    payload.update(extra)
    return payload


class JobAssignmentTests(OwnerTestCase):
    entity_type = "owner_job_assignment"

    def test_the_owner_assigns_a_job_and_the_server_fills_in_the_rest(self):
        self.assertAccepted(self.push("J1", job(scope="school", assignedByMembershipId="someone-else")))
        stored = self.stored("J1").payload
        self.assertEqual(stored["scope"], "section")  # worked out, not taken from the app
        self.assertEqual(stored["assignedByMembershipId"], str(self.owner.id))
        self.assertEqual(stored["duties"], ["academics.teaching", "administration.students"])
        self.assertIsNone(stored["membershipId"])
        self.assertEqual(stored["status"], "pendingActivation")

    def test_nobody_but_the_owner_may_assign_jobs(self):
        for role, member in self.members.items():
            if role != "proprietor":
                self.assertRejected(self.push("J1", job(), who=member), "role")
        self.assertEqual(SyncRecord.objects.count(), 0)

    def test_a_whole_school_job_has_no_section(self):
        self.assertAccepted(self.push("J1", job(role="finance", sectionId="", sectionName="", duties=["finance.fees"])))
        stored = self.stored("J1").payload
        self.assertEqual((stored["scope"], stored["sectionId"], stored["sectionName"]), ("school", None, None))

    def test_a_head_of_section_needs_a_section(self):
        self.assertRejected(self.push("J1", job(sectionId="")), "sectionId")

    def test_registered_and_unregistered_people(self):
        # Registered: the staff record identifies them, an email is optional.
        self.assertAccepted(self.push("J1", job()))
        self.assertRejected(self.push("J2", job(registeredStaffId="")), "registeredStaffId")
        # Unregistered: an email is how they are reached, and they have no staff record.
        unregistered = job(recipientType="unregistered", registeredStaffId="", email="Bala@School.NG")
        self.assertAccepted(self.push("J3", unregistered))
        self.assertEqual(self.stored("J3").payload["email"], "bala@school.ng")
        self.assertRejected(self.push("J4", job(recipientType="unregistered", registeredStaffId="", email="")), "email")
        self.assertRejected(self.push("J5", job(recipientType="unregistered", email="bala@school.ng")), "staff record")
        self.assertRejected(self.push("J6", job(email="not-an-email")), "email")
        self.assertRejected(self.push("J7", job(recipientType="alien")), "recipientType")

    def test_unknown_roles_and_duties_are_refused(self):
        self.assertRejected(self.push("J1", job(role="emperor")), "role")
        self.assertRejected(self.push("J1", job(duties=["finance.fees", "root.everything"])), "root.everything")
        self.assertRejected(self.push("J1", job(duties=[])), "at least")
        self.assertRejected(self.push("J1", job(title=" ")), "title")
        self.assertRejected(self.push("J1", job(name="")), "name")

    def test_every_duty_and_role_the_app_offers_is_accepted(self):
        from apps.owner.jobs.constants import DUTIES, JOB_ROLES

        self.assertEqual(len(DUTIES), 24)
        every_duty = sorted(DUTIES)
        for i, role in enumerate(sorted(JOB_ROLES - {"sectionHead"})):
            self.assertAccepted(self.push(f"J{i}", job(role=role, sectionId="", duties=every_duty)))

    def test_the_app_cannot_activate_an_assignment_or_link_an_account(self):
        self.assertRejected(self.push("J1", job(status="active")), "activating")
        someone = str(self.members["teacher"].id)
        self.assertAccepted(self.push("J1", job(membershipId=someone)))
        self.assertIsNone(self.stored("J1").payload["membershipId"])

    def test_an_active_assignment_stays_linked_when_edited_or_revoked(self):
        teacher = self.members["teacher"]
        self.push("J1", job())
        record = self.stored("J1")
        record.payload = {**record.payload, "status": "active", "membershipId": str(teacher.id)}
        record.save()
        self.assertAccepted(self.push("J1", job(status="active", title="Head of Primary (acting)"), operation="update"))
        stored = self.stored("J1").payload
        self.assertEqual((stored["status"], stored["membershipId"]), ("active", str(teacher.id)))
        self.assertAccepted(self.push("J1", job(status="revoked"), operation="update"))
        stored = self.stored("J1").payload
        self.assertEqual((stored["status"], stored["membershipId"]), ("revoked", str(teacher.id)))
        self.assertEqual(stored["revokedByMembershipId"], str(self.owner.id))

    def test_a_job_cannot_be_deleted_only_revoked(self):
        self.push("J1", job())
        self.assertRejected(self.push("J1", operation="delete"), "deleted")
