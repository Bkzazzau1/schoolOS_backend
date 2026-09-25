"""The Administrator's front-desk modules: only the Administrator writes, leadership reads."""

from apps.schoollife.framework import SchoolLifeHandler
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
    def test_every_module_is_registered(self):
        for spec in SPECS:
            self.assertIsInstance(registry.get(spec.entity_type), SchoolLifeHandler, spec.entity_type)
        self.assertEqual(len({s.entity_type for s in SPECS}), len(SPECS))

    def test_the_administrator_can_add_every_module_and_the_server_stamps_it(self):
        admin = self.members["administrator"]
        for spec in SPECS:
            self.ok(self.send(spec, admin))
            p = self.stored_of(spec)
            self.assertEqual((p["createdByMembershipId"], p["updatedByMembershipId"]), (str(admin.id),) * 2, spec.entity_type)
            self.assertTrue(p["createdAt"] and p["updatedAt"])

    def test_no_other_role_may_write_any_of_it(self):
        for spec in SPECS:
            for role in ("proprietor", "principal", "teacher", "accountant", "parent", "student", "staff", "driver"):
                self.rejected(self.send(spec, self.members[role]), "role may not")
                self.assertFalse(SyncRecord.objects.filter(entity_type=spec.entity_type).exists(), (spec.entity_type, role))

    def test_required_fields_are_required(self):
        admin = self.members["administrator"]
        for spec in SPECS:
            for name in spec.required:
                self.rejected(self.send(spec, admin, payload=sample(spec, **{name: "  "})), name)

    def test_only_school_leadership_reads_any_of_it(self):
        admin = self.members["administrator"]
        for spec in SPECS:
            self.ok(self.send(spec, admin, "r1"))
        kinds_seen = lambda who: {t for t, _ in self.pulled(who)}
        for role in ("proprietor", "principal", "administrator"):
            seen = kinds_seen(self.members[role])
            for spec in SPECS:
                self.assertIn(spec.entity_type, seen, (spec.entity_type, role))
        for role in ("teacher", "accountant", "parent", "student", "staff", "driver"):
            seen = kinds_seen(self.members[role])
            for spec in SPECS:
                self.assertNotIn(spec.entity_type, seen, (spec.entity_type, role))


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
