"""The school office's own operational modules: who writes and who reads varies per module."""

from apps.schoollife.framework import EVERYONE, SchoolLifeHandler
from apps.staff.tests.helpers import StaffTestCase
from apps.sync import registry
from apps.sync.models import SyncRecord

from ..specs import SPECS


def sample(spec, entity_id="rec-1", **over):
    """The smallest valid record for a module."""
    body = {name: "Something" for name in spec.required}
    if spec.id_field:
        body[spec.id_field] = entity_id
    body.update(spec.defaults)
    body.update(over)
    return body


class ModuleTestCase(StaffTestCase):
    def send(self, spec, who, entity_id="rec-1", payload=None, **kw):
        payload = sample(spec, entity_id) if payload is None else payload
        return self.push(spec.entity_type, entity_id, payload, who=who, **kw)

    def stored_of(self, spec, entity_id="rec-1"):
        return self.stored(spec.entity_type, entity_id).payload

    def pulled(self, who):
        self.client.force_authenticate(who.user)
        found = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id)}).json()["records"]
        return {(r["entityType"], r["entityId"]) for r in found}


class EveryModuleTests(ModuleTestCase):
    def test_every_module_is_registered_and_none_is_left_open(self):
        for spec in SPECS:
            self.assertIsInstance(registry.get(spec.entity_type), SchoolLifeHandler, spec.entity_type)
        self.assertEqual(len({s.entity_type for s in SPECS}), len(SPECS))

    def test_its_own_manager_can_add_each_module_and_the_server_stamps_it(self):
        for spec in SPECS:
            manager = self.members[sorted(spec.manage)[0]]
            self.ok(self.send(spec, manager))
            p = self.stored_of(spec)
            self.assertEqual((p["createdByMembershipId"], p["updatedByMembershipId"]), (str(manager.id),) * 2, spec.entity_type)
            self.assertTrue(p["createdAt"] and p["updatedAt"])

    def test_people_without_a_role_in_the_module_cannot_write(self):
        for spec in SPECS:
            for role in EVERYONE - spec.manage - spec.contribute:
                self.rejected(self.send(spec, self.members[role]), "role may not")
                self.assertFalse(SyncRecord.objects.filter(entity_type=spec.entity_type).exists(), (spec.entity_type, role))

    def test_required_fields_are_required(self):
        for spec in SPECS:
            manager = self.members[sorted(spec.manage)[0]]
            for name in spec.required:
                self.rejected(self.send(spec, manager, payload=sample(spec, **{name: "  "})), name)

    def test_reads_go_only_to_the_roles_each_module_names(self):
        for spec in SPECS:
            manager = self.members[sorted(spec.manage)[0]]
            self.ok(self.send(spec, manager, "r1"))
            for role in EVERYONE:
                seen = spec.entity_type in {t for t, _ in self.pulled(self.members[role])}
                expected = role in spec.manage or role in spec.read
                self.assertEqual(seen, expected, (spec.entity_type, role))


class ParentMessageContributorTests(ModuleTestCase):
    """A parent may add a message toward their child's class channel; the whole staff side reads it."""

    def test_a_parent_can_add_their_own_message_and_a_teacher_can_read_it(self):
        from apps.schoollife.specs.communications import PARENT_MESSAGE

        parent = self.members["parent"]
        self.ok(self.send(PARENT_MESSAGE, parent, "msg-1"))
        self.assertIn(("parent_message", "msg-1"), self.pulled(self.members["teacher"]))
        self.assertNotIn(("parent_message", "msg-1"), self.pulled(self.members["student"]))


class AttendanceEventIdentityTests(ModuleTestCase):
    """The storage id is derived (time+student, or a synthetic slot), never a field the payload must echo."""

    def test_an_attendance_event_does_not_need_a_matching_id_field(self):
        from ..specs import ATTENDANCE_EVENTS

        admin = self.members["administrator"]
        # The app derives this same id client-side (see AdministratorAttendanceEvent.entityId):
        # time and student, slugified into a safe sync id.
        payload = {"time": "07:45", "student": "Maryam Abdullahi", "className": "JSS 2A", "date": "2026-09-25", "entityKey": ""}
        self.ok(self.send(ATTENDANCE_EVENTS, admin, "07-45-maryam-abdullahi", payload=payload))

    def test_an_operations_task_does_not_need_a_matching_id_field(self):
        from ..specs import OPERATIONS_QUEUE

        admin = self.members["administrator"]
        payload = {"title": "Applications awaiting review", "count": "3", "note": ""}
        self.ok(self.send(OPERATIONS_QUEUE, admin, "operations-1", payload=payload))


class AttendanceDeviceIdentityTests(ModuleTestCase):
    """A device's display name ("Main Gate Face Terminal") is not itself a safe sync id."""

    def test_a_device_with_a_display_name_containing_spaces_can_still_be_stored(self):
        from ..specs import ATTENDANCE_DEVICES

        admin = self.members["administrator"]
        payload = {"name": "Main Gate Face Terminal", "location": "Main entrance", "type": "Face", "status": "Online", "lastEvent": "", "events": "0"}
        self.ok(self.send(ATTENDANCE_DEVICES, admin, "main-gate-face-terminal", payload=payload))
        self.assertEqual(self.stored_of(ATTENDANCE_DEVICES, "main-gate-face-terminal")["name"], "Main Gate Face Terminal")


class PrincipalTeacherNoteTests(ModuleTestCase):
    """A Principal's private note about a teacher: not even the owner reads it."""

    def test_a_principal_can_save_and_clear_a_note_and_no_one_else_ever_reads_it(self):
        from ..specs import PRINCIPAL_TEACHER_NOTES

        principal = self.members["principal"]
        self.ok(self.send(PRINCIPAL_TEACHER_NOTES, principal, "teacher-1", payload={"teacherId": "teacher-1", "text": "Excellent CA moderation this term."}))
        for role in EVERYONE - {"principal"}:
            self.assertNotIn(("principal_teacher_note", "teacher-1"), self.pulled(self.members[role]), role)
        self.assertIn(("principal_teacher_note", "teacher-1"), self.pulled(principal))

        # Clearing the note (empty text) is a real, supported action, not a validation error.
        self.ok(self.push(PRINCIPAL_TEACHER_NOTES.entity_type, "teacher-1", {"teacherId": "teacher-1", "text": ""}, operation="update", who=principal))
        self.assertEqual(self.stored_of(PRINCIPAL_TEACHER_NOTES, "teacher-1")["text"], "")
