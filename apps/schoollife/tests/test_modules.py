from apps.schoollife.framework import EVERYONE, MAX_BYTES, SchoolLifeHandler
from apps.schoollife.specs import SPECS
from apps.staff.tests.helpers import StaffTestCase
from apps.sync import registry
from apps.sync.models import SyncRecord


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
    """The guarantees every module gets from the shared handler."""

    def test_every_module_is_registered_and_none_is_left_open(self):
        for spec in SPECS:
            self.assertIsInstance(registry.get(spec.entity_type), SchoolLifeHandler, spec.entity_type)
        self.assertEqual(len({s.entity_type for s in SPECS}), len(SPECS))

    def test_managers_can_add_and_the_server_stamps_it(self):
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

    def test_stamps_cannot_be_forged(self):
        for spec in SPECS:
            manager = self.members[sorted(spec.manage)[0]]
            forged = sample(spec, createdByMembershipId=str(self.members["parent"].id), createdAt="2001-01-01",
                            updatedByMembershipId="x", updatedAt="2001-01-01")
            self.ok(self.send(spec, manager, payload=forged))
            p = self.stored_of(spec)
            self.assertEqual((p["createdByMembershipId"], p["createdAt"] > "2020"), (str(manager.id), True), spec.entity_type)

    def test_the_id_field_must_match_the_record(self):
        for spec in SPECS:
            if spec.id_field:
                manager = self.members[sorted(spec.manage)[0]]
                self.rejected(self.send(spec, manager, payload=sample(spec, "other-id")), "must match")

    def test_required_fields_are_required(self):
        for spec in SPECS:
            manager = self.members[sorted(spec.manage)[0]]
            for name in spec.required:
                self.rejected(self.send(spec, manager, payload=sample(spec, **{name: "  "})), name)

    def test_oversized_records_and_text_are_refused(self):
        for spec in SPECS:
            manager = self.members[sorted(spec.manage)[0]]
            self.rejected(self.send(spec, manager, payload=sample(spec, note="x" * 6000)), "too long")
            self.rejected(self.send(spec, manager, payload=sample(spec, extra=[{"k": "y" * 4000}] * 20)), "too large")
        self.assertGreater(MAX_BYTES, 1000)

    def test_records_are_not_deleted(self):
        for spec in SPECS:
            manager = self.members[sorted(spec.manage)[0]]
            self.ok(self.send(spec, manager))
            self.rejected(self.push(spec.entity_type, "rec-1", operation="delete", who=manager), "cannot be deleted")

    def test_another_school_cannot_write_or_read_them(self):
        for spec in SPECS:
            self.assertEqual(self.push(spec.entity_type, "rec-1", sample(spec), who=self.other_owner, school=self.school).status_code, 403)
        manager = self.owner
        for spec in SPECS:
            self.ok(self.send(spec, manager, entity_id=f"id-{spec.entity_type}", payload=sample(spec, f"id-{spec.entity_type}")))
        self.client.force_authenticate(self.other_owner.user)
        found = self.client.get("/api/v1/sync/pull/", {"school": str(self.other_school.id)}).json()["records"]
        self.assertEqual(found, [])

    def test_managers_can_change_a_record_and_a_stale_device_gets_a_conflict(self):
        for spec in SPECS:
            manager = self.members[sorted(spec.manage)[0]]
            self.ok(self.send(spec, manager))
            self.ok(self.push(spec.entity_type, "rec-1", sample(spec, note="edited"), operation="update", who=manager))
            self.assertEqual(self.stored_of(spec)["note"], "edited")
            self.assertEqual(self.push(spec.entity_type, "rec-1", sample(spec), operation="update", who=manager, base_version=99).status_code, 409)
