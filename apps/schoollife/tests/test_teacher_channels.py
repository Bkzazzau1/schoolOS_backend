"""A real teacher's private channel to school leadership, and a real, shared channel per real
subject a teacher currently teaches - apps/schoollife/messaging/teacher_channels.py.
"""

from datetime import datetime, timezone

from django.contrib.auth import get_user_model

from apps.academics.models import AcademicClass, AcademicSession, ClassSubject, Subject, TeachingAssignment
from apps.schoollife.messaging.teacher_channels import (
    TEACHER_DEPARTMENT_MESSAGE,
    TEACHER_DEPARTMENT_RECEIPT,
    TEACHER_LEADERSHIP_MESSAGE,
    TEACHER_LEADERSHIP_RECEIPT,
)
from apps.schools.models import Membership, Role
from apps.staff.tests.helpers import StaffTestCase
from apps.sync.models import SyncRecord


class TeacherChannelsTestCase(StaffTestCase):
    """A real teacher teaching real Mathematics classes, a real co-teacher sharing the same real
    Mathematics subject in a different class, and a real unrelated teacher teaching real English -
    to prove the department channel reaches real peers and nobody else."""

    def setUp(self):
        super().setUp()
        self.teacher, self.principal = self.members["teacher"], self.members["principal"]
        self.co_teacher = self._extra_membership("co-teacher@school.ng", Role.TEACHER)
        self.other_teacher = self._extra_membership("other-teacher@school.ng", Role.TEACHER)

        self.maths = self._subject("MATH", "Mathematics")
        self.english = self._subject("ENG", "English")
        self._teach(self.teacher, self.maths, "JSS 2A")
        self._teach(self.co_teacher, self.maths, "JSS 2B")
        self._teach(self.other_teacher, self.english, "JSS 2C")

        self.leadership_thread_id = f"leadership-thread-{self.teacher.id}"
        self.department_thread_id = f"department-thread-{self.maths.code}"
        self.other_department_thread_id = f"department-thread-{self.english.code}"

    def _extra_membership(self, email, role):
        user = get_user_model().objects.create_user(email, "a-long-test-password-1")
        return Membership.objects.create(user=user, school=self.school, role=role)

    def _subject(self, code, name):
        return Subject.objects.create(school=self.school, code=code, name=name, section="Secondary")

    def _teach(self, teacher_membership, subject, class_code):
        session = AcademicSession.objects.create(
            school=self.school, code=f"S-{class_code}", name="2026/2027",
            starts_on=datetime(2026, 1, 1).date(), ends_on=datetime(2026, 12, 31).date(),
        )
        academic_class = AcademicClass.objects.create(
            school=self.school, code=class_code, name=class_code, section="Secondary", level_order=1,
        )
        class_subject = ClassSubject.objects.create(session=session, academic_class=academic_class, subject=subject)
        TeachingAssignment.objects.create(
            school=self.school, external_id=f"TA-{class_code}", class_subject=class_subject,
            teacher_membership=teacher_membership, started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    def pulled(self, who):
        self.client.force_authenticate(who.user)
        found = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id)}).json()["records"]
        return {(r["entityType"], r["entityId"]) for r in found}


class LeadershipMessageTests(TeacherChannelsTestCase):
    def message(self, who, id="MSG-1", thread_id=None, body="Hello", operation="create", **over):
        payload = {"id": id, "threadId": thread_id or self.leadership_thread_id, "body": body, **over}
        return self.push(TEACHER_LEADERSHIP_MESSAGE, id, payload, who=who, operation=operation)

    def stored_of(self, id):
        return SyncRecord.objects.get(school=self.school, entity_type=TEACHER_LEADERSHIP_MESSAGE, entity_id=id).payload

    def test_the_real_teacher_can_message_their_own_leadership_thread(self):
        self.ok(self.message(self.teacher))
        self.assertEqual(self.stored_of("MSG-1")["authorRole"], "teacher")

    def test_a_manager_can_reply(self):
        for role in ("proprietor", "administrator", "principal"):
            self.ok(self.message(self.members[role], id=f"MSG-{role}"))

    def test_a_different_teacher_cannot_write_into_someone_elses_leadership_thread(self):
        self.rejected(self.message(self.co_teacher), "not a conversation")

    def test_an_unrelated_role_cannot_message_here_at_all(self):
        for role in ("student", "staff", "accountant", "driver", "parent"):
            self.rejected(self.message(self.members[role], id=f"MSG-{role}"), "role may not")

    def test_an_unknown_thread_is_refused(self):
        self.rejected(self.message(self.teacher, thread_id="leadership-thread-ghost"), "not found")

    def test_a_message_is_never_changed_or_removed_once_sent(self):
        self.ok(self.message(self.teacher))
        self.rejected(self.message(self.teacher, id="MSG-1", body="Edited", operation="update"), "cannot be changed")
        self.rejected(self.push(TEACHER_LEADERSHIP_MESSAGE, "MSG-1", operation="delete", who=self.teacher), "cannot be changed")

    def test_only_the_real_teacher_and_managers_read_it_nobody_else_does(self):
        self.ok(self.message(self.teacher))
        seen = lambda who: (TEACHER_LEADERSHIP_MESSAGE, "MSG-1") in self.pulled(who)  # noqa: E731
        self.assertTrue(seen(self.teacher))
        self.assertTrue(seen(self.principal))
        self.assertFalse(seen(self.co_teacher))
        self.assertFalse(seen(self.members["student"]))

    def test_another_school_cannot_read_or_write(self):
        payload = {"id": "MSG-X", "threadId": self.leadership_thread_id, "body": "Hello"}
        response = self.push(TEACHER_LEADERSHIP_MESSAGE, "MSG-X", payload, who=self.other_owner, school=self.school)
        self.assertEqual(response.status_code, 403)


class DepartmentMessageTests(TeacherChannelsTestCase):
    def message(self, who, id="MSG-1", thread_id=None, body="Hello", operation="create", **over):
        payload = {"id": id, "threadId": thread_id or self.department_thread_id, "body": body, **over}
        return self.push(TEACHER_DEPARTMENT_MESSAGE, id, payload, who=who, operation=operation)

    def stored_of(self, id):
        return SyncRecord.objects.get(school=self.school, entity_type=TEACHER_DEPARTMENT_MESSAGE, entity_id=id).payload

    def test_a_real_teacher_of_this_subject_can_post(self):
        self.ok(self.message(self.teacher))
        self.assertEqual(self.stored_of("MSG-1")["authorRole"], "teacher")

    def test_a_real_peer_teaching_the_same_subject_in_a_different_class_can_also_post(self):
        self.ok(self.message(self.co_teacher, id="MSG-2"))

    def test_a_manager_can_post_for_oversight(self):
        self.ok(self.message(self.principal))

    def test_a_teacher_who_does_not_teach_this_subject_cannot_post(self):
        self.rejected(self.message(self.other_teacher), "not a conversation")

    def test_an_unrelated_role_cannot_post_at_all(self):
        for role in ("student", "staff", "accountant", "driver", "parent"):
            self.rejected(self.message(self.members[role], id=f"MSG-{role}"), "role may not")

    def test_an_unknown_subject_is_refused(self):
        self.rejected(self.message(self.teacher, thread_id="department-thread-GHOST"), "not found")

    def test_a_message_is_never_changed_or_removed_once_sent(self):
        self.ok(self.message(self.teacher))
        self.rejected(self.message(self.teacher, id="MSG-1", body="Edited", operation="update"), "cannot be changed")
        self.rejected(self.push(TEACHER_DEPARTMENT_MESSAGE, "MSG-1", operation="delete", who=self.teacher), "cannot be changed")

    def test_real_peers_and_managers_read_it_an_unrelated_teacher_never_does(self):
        self.ok(self.message(self.teacher))
        seen = lambda who: (TEACHER_DEPARTMENT_MESSAGE, "MSG-1") in self.pulled(who)  # noqa: E731
        self.assertTrue(seen(self.teacher))
        self.assertTrue(seen(self.co_teacher))
        self.assertTrue(seen(self.principal))
        self.assertFalse(seen(self.other_teacher))

    def test_two_departments_never_cross(self):
        self.ok(self.message(self.teacher, id="MSG-1"))
        self.ok(self.message(self.other_teacher, id="MSG-2", thread_id=self.other_department_thread_id))
        self.assertEqual({i for t, i in self.pulled(self.teacher) if t == TEACHER_DEPARTMENT_MESSAGE}, {"MSG-1"})
        self.assertEqual({i for t, i in self.pulled(self.other_teacher) if t == TEACHER_DEPARTMENT_MESSAGE}, {"MSG-2"})


class ReceiptTests(TeacherChannelsTestCase):
    def test_the_real_teacher_and_a_manager_can_each_mark_their_own_leadership_receipt(self):
        rid = f"{self.teacher.id}:thread-seen:{self.leadership_thread_id}:1758790000000000"
        payload = {"id": rid, "threadId": self.leadership_thread_id, "seenAt": "2026-09-25T06:46:00Z"}
        self.ok(self.push(TEACHER_LEADERSHIP_RECEIPT, rid, payload, who=self.teacher))
        stored = SyncRecord.objects.get(school=self.school, entity_type=TEACHER_LEADERSHIP_RECEIPT, entity_id=rid).payload
        self.assertEqual((stored["threadId"], stored["membershipId"]), (self.leadership_thread_id, str(self.teacher.id)))
        self.rejected(self.push(TEACHER_LEADERSHIP_RECEIPT, rid, payload, who=self.teacher, operation="update"), "never changed")

    def test_a_different_teacher_cannot_mark_someone_elses_leadership_receipt(self):
        rid = f"{self.co_teacher.id}:thread-seen:{self.leadership_thread_id}:1758790000000000"
        payload = {"id": rid, "threadId": self.leadership_thread_id, "seenAt": ""}
        self.rejected(self.push(TEACHER_LEADERSHIP_RECEIPT, rid, payload, who=self.co_teacher), "not a conversation")

    def test_a_real_peer_can_mark_their_own_department_receipt(self):
        rid = f"{self.co_teacher.id}:thread-seen:{self.department_thread_id}:1758790000000000"
        payload = {"id": rid, "threadId": self.department_thread_id, "seenAt": "2026-09-25T06:46:00Z"}
        self.ok(self.push(TEACHER_DEPARTMENT_RECEIPT, rid, payload, who=self.co_teacher))

    def test_an_unrelated_teacher_cannot_mark_a_department_receipt(self):
        rid = f"{self.other_teacher.id}:thread-seen:{self.department_thread_id}:1758790000000000"
        payload = {"id": rid, "threadId": self.department_thread_id, "seenAt": ""}
        self.rejected(self.push(TEACHER_DEPARTMENT_RECEIPT, rid, payload, who=self.other_teacher), "not a conversation")

    def test_a_forged_receipt_id_is_refused(self):
        rid = f"{self.other_teacher.id}:thread-seen:{self.leadership_thread_id}:1758790000000000"
        payload = {"id": rid, "threadId": self.leadership_thread_id, "seenAt": ""}
        self.rejected(self.push(TEACHER_LEADERSHIP_RECEIPT, rid, payload, who=self.teacher), "does not belong")
