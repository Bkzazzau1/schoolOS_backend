"""A parent's real messages toward their child's class, and real replies from the child's real current class
teacher - apps/schoollife/messaging/parent_messages.py. Unlike community_post (any adult member, moderated by
role alone), this one is scoped to a real family and a real class: every test here stands up a real Student,
GuardianLink, StudentEnrollment and TeachingAssignment, the same shape apps/weekly_learning/visibility.py's own
authorization already relies on, and proves the handler only reaches the two real people it should.
"""

from datetime import datetime, timezone

from django.contrib.auth import get_user_model

from apps.academics.models import AcademicClass, AcademicSession, ClassSubject, Subject, TeachingAssignment
from apps.schoollife.messaging.parent_messages import PARENT_MESSAGE, PARENT_MESSAGE_RECEIPT
from apps.schools.models import Membership, Role
from apps.staff.tests.helpers import StaffTestCase
from apps.students.models import GuardianLink, Student, StudentEnrollment
from apps.sync.models import SyncRecord


class ParentMessageTestCase(StaffTestCase):
    """One real family in one real class, with its own real guardian and its own real class teacher - and a
    second, unrelated family/teacher in the same school, to prove the handler never leaks across either."""

    def setUp(self):
        super().setUp()
        self.parent, self.teacher, self.principal = self.members["parent"], self.members["teacher"], self.members["principal"]
        self.other_teacher = self._extra_membership("other-teacher@school.ng", Role.TEACHER)
        self.other_parent = self._extra_membership("other-parent@school.ng", Role.PARENT)

        self.class_ = self._class("JSS 2A")
        self.student = self._student("STU-001", self.class_)
        GuardianLink.objects.create(student=self.student, account_user=self.parent.user, name="Amina Bello", phone="0803000001")
        self._teach(self.teacher, self.class_)

        self.other_class = self._class("JSS 2B")
        self.other_student = self._student("STU-002", self.other_class)
        self._teach(self.other_teacher, self.other_class)

        self.thread_id = f"channel-{self.student.student_code}"
        self.other_thread_id = f"channel-{self.other_student.student_code}"

    def _extra_membership(self, email, role):
        user = get_user_model().objects.create_user(email, "a-long-test-password-1")
        return Membership.objects.create(user=user, school=self.school, role=role)

    def _class(self, name):
        return AcademicClass.objects.create(school=self.school, code=name, name=name, section="Secondary", level_order=1)

    def _student(self, code, academic_class):
        student = Student.objects.create(
            school=self.school, admission_number=code, student_code=code, first_name="Test", surname="Student",
        )
        StudentEnrollment.objects.create(
            school=self.school, student=student, academic_section="Secondary", class_name=academic_class.name,
            started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        return student

    def _teach(self, teacher_membership, academic_class):
        session = AcademicSession.objects.create(
            school=self.school, code=f"S-{academic_class.code}", name="2026/2027",
            starts_on=datetime(2026, 1, 1).date(), ends_on=datetime(2026, 12, 31).date(),
        )
        subject = Subject.objects.create(
            school=self.school, code=f"SUB-{academic_class.code}", name=f"Mathematics ({academic_class.code})", section="Secondary",
        )
        class_subject = ClassSubject.objects.create(session=session, academic_class=academic_class, subject=subject)
        TeachingAssignment.objects.create(
            school=self.school, external_id=f"TA-{academic_class.code}", class_subject=class_subject,
            teacher_membership=teacher_membership, started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    def message(self, who, id="MSG-1", thread_id=None, body="Hello", operation="create", school=None, **over):
        payload = {"id": id, "threadId": thread_id or self.thread_id, "body": body, **over}
        return self.push(PARENT_MESSAGE, id, payload, who=who, operation=operation, school=school)

    def stored_of(self, id):
        return SyncRecord.objects.get(school=self.school, entity_type=PARENT_MESSAGE, entity_id=id).payload

    def pulled(self, who):
        self.client.force_authenticate(who.user)
        found = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id)}).json()["records"]
        return {(r["entityType"], r["entityId"]) for r in found}


class WhoMayWriteTests(ParentMessageTestCase):
    def test_the_real_guardian_can_message_their_childs_thread(self):
        self.ok(self.message(self.parent))
        self.assertEqual(self.stored_of("MSG-1")["authorRole"], "parent")

    def test_the_real_class_teacher_can_reply(self):
        self.ok(self.message(self.teacher, id="MSG-2", body="Thank you for letting us know."))
        self.assertEqual(self.stored_of("MSG-2")["authorRole"], "teacher")

    def test_a_manager_can_write_for_oversight(self):
        for role in ("proprietor", "administrator", "principal"):
            self.ok(self.message(self.members[role], id=f"MSG-{role}"))

    def test_a_parent_who_is_not_this_childs_guardian_cannot_write_here(self):
        self.rejected(self.message(self.other_parent), "not a conversation")

    def test_a_teacher_who_does_not_teach_this_class_cannot_write_here(self):
        self.rejected(self.message(self.other_teacher), "not a conversation")

    def test_a_teacher_who_teaches_a_different_class_cannot_write_into_the_wrong_thread(self):
        self.rejected(self.message(self.teacher, thread_id=self.other_thread_id), "not a conversation")

    def test_students_staff_accountants_and_drivers_cannot_message_here_at_all(self):
        for role in ("student", "staff", "accountant", "driver"):
            self.rejected(self.message(self.members[role], id=f"MSG-{role}"), "role may not")

    def test_an_unknown_thread_is_refused(self):
        self.rejected(self.message(self.parent, thread_id="channel-GHOST"), "not found")


class ServerStampingTests(ParentMessageTestCase):
    def test_author_role_and_membership_are_the_servers_never_the_apps(self):
        self.ok(self.message(self.parent, authorRole="teacher", authorMembershipId=str(self.teacher.id), createdAt="2001-01-01"))
        p = self.stored_of("MSG-1")
        self.assertEqual((p["authorRole"], p["authorMembershipId"]), ("parent", str(self.parent.id)))
        self.assertNotEqual(p["createdAt"], "2001-01-01")

    def test_a_message_is_never_changed_or_removed_once_sent(self):
        self.ok(self.message(self.parent))
        self.rejected(self.message(self.teacher, id="MSG-1", body="Edited", operation="update"), "cannot be changed")
        self.rejected(self.push(PARENT_MESSAGE, "MSG-1", operation="delete", who=self.parent), "cannot be changed")

    def test_bad_messages_are_refused(self):
        for over in ({"body": ""}, {"body": "x" * 4001}, {"threadId": ""}):
            self.rejected(self.message(self.parent, **over))

    def test_a_payload_id_that_does_not_match_the_record_is_refused(self):
        payload = {"id": "other", "threadId": self.thread_id, "body": "Hello"}
        self.rejected(self.push(PARENT_MESSAGE, "MSG-1", payload, who=self.parent), "must match")


class WhoMayReadTests(ParentMessageTestCase):
    def test_the_real_guardian_and_the_real_class_teacher_both_see_it_nobody_else_does(self):
        self.ok(self.message(self.parent))
        seen = lambda who: (PARENT_MESSAGE, "MSG-1") in self.pulled(who)  # noqa: E731
        self.assertTrue(seen(self.parent))
        self.assertTrue(seen(self.teacher))
        self.assertTrue(seen(self.principal))  # a manager, for oversight
        self.assertFalse(seen(self.other_teacher))
        self.assertFalse(seen(self.members["student"]))
        self.assertFalse(seen(self.members["accountant"]))

    def test_two_familes_conversations_never_cross(self):
        self.ok(self.message(self.parent, id="MSG-1"))
        self.ok(self.message(self.other_teacher, id="MSG-2", thread_id=self.other_thread_id))
        self.assertEqual({i for t, i in self.pulled(self.teacher) if t == PARENT_MESSAGE}, {"MSG-1"})
        self.assertEqual({i for t, i in self.pulled(self.other_teacher) if t == PARENT_MESSAGE}, {"MSG-2"})

    def test_another_school_cannot_read_or_write(self):
        payload = {"id": "MSG-X", "threadId": self.thread_id, "body": "Hello"}
        response = self.push(PARENT_MESSAGE, "MSG-X", payload, who=self.other_owner, school=self.school)
        self.assertEqual(response.status_code, 403)


class ReceiptTests(ParentMessageTestCase):
    """A real 'I have seen this thread' receipt - what lets a thread's own unread state be real."""

    def receipt_id(self, who, thread_id=None, epoch="1758790000000000"):
        return f"{who.id}:thread-seen:{thread_id or self.thread_id}:{epoch}"

    def receipt(self, who, thread_id=None, **over):
        thread_id = thread_id or self.thread_id
        rid = self.receipt_id(who, thread_id)
        payload = {"id": rid, "threadId": thread_id, "seenAt": "2026-09-25T06:46:00Z", **over}
        return self.push(PARENT_MESSAGE_RECEIPT, rid, payload, who=who)

    def test_the_real_guardian_and_the_real_class_teacher_can_each_mark_their_own_receipt(self):
        self.ok(self.receipt(self.parent))
        self.ok(self.receipt(self.teacher))
        stored_parent = self.stored_of_receipt(self.receipt_id(self.parent))
        self.assertEqual((stored_parent["threadId"], stored_parent["membershipId"]), (self.thread_id, str(self.parent.id)))

    def test_a_manager_can_mark_a_receipt_for_oversight(self):
        self.ok(self.receipt(self.principal))

    def test_nobody_outside_the_conversation_can_mark_a_receipt(self):
        self.rejected(self.receipt(self.other_parent), "not a conversation")
        self.rejected(self.receipt(self.other_teacher), "not a conversation")

    def test_an_unrelated_role_cannot_mark_a_receipt_at_all(self):
        self.rejected(self.receipt(self.members["student"]), "role may not")

    def test_a_receipt_is_never_changed_once_recorded(self):
        self.ok(self.receipt(self.parent))
        rid = self.receipt_id(self.parent)
        payload = {"id": rid, "threadId": self.thread_id, "seenAt": "2026-09-25T06:46:00Z"}
        self.rejected(self.push(PARENT_MESSAGE_RECEIPT, rid, payload, who=self.parent, operation="update"), "never changed")
        self.rejected(self.push(PARENT_MESSAGE_RECEIPT, rid, operation="delete", who=self.parent), "never changed")

    def test_a_forged_receipt_id_is_refused(self):
        rid = f"{self.other_parent.id}:thread-seen:{self.thread_id}:1758790000000000"
        payload = {"id": rid, "threadId": self.thread_id, "seenAt": ""}
        self.rejected(self.push(PARENT_MESSAGE_RECEIPT, rid, payload, who=self.parent), "does not belong")

    def test_only_the_person_who_made_a_receipt_and_managers_read_it_back(self):
        self.ok(self.receipt(self.parent))
        seen = lambda who: (PARENT_MESSAGE_RECEIPT, self.receipt_id(self.parent)) in self.pulled(who)  # noqa: E731
        self.assertTrue(seen(self.parent))
        self.assertTrue(seen(self.principal))
        self.assertFalse(seen(self.teacher))
        self.assertFalse(seen(self.other_parent))

    def stored_of_receipt(self, receipt_id):
        return SyncRecord.objects.get(school=self.school, entity_type=PARENT_MESSAGE_RECEIPT, entity_id=receipt_id).payload
