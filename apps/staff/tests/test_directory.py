from apps.staff.models import IdentityClaim

from .helpers import StaffTestCase


def entry(**over):
    payload = {"id": "STAFF-100", "name": "Mr. Bala Sani", "role": "Driver", "section": "Transport",
               "fileStatus": "Missing document"}
    payload.update(over)
    return payload


class StaffDirectoryTests(StaffTestCase):
    def add(self, **over):
        payload = entry(**over)
        return self.push("administrator_staff_directory", payload["id"], payload, who=self.owner)

    def test_only_the_owner_adds_people_directly(self):
        self.ok(self.add())
        stored = self.stored("administrator_staff_directory", "STAFF-100").payload
        self.assertEqual((stored["staffCategory"], stored["createdByMembershipId"]), ("added_by_owner", str(self.owner.id)))
        for role in ["principal", "administrator", "accountant", "teacher", "staff", "parent", "student"]:
            payload = entry(id=f"STAFF-{role}")
            response = self.push("administrator_staff_directory", payload["id"], payload, who=self.members[role])
            self.rejected(response)
        # The administrator may edit but not add, and is told what to do instead.
        self.assertIn("proposes", self.push("administrator_staff_directory", "S9", entry(id="S9"),
                                            who=self.members["administrator"]).json()["message"])

    def test_the_owner_edits_anything(self):
        self.ok(self.add())
        self.ok(self.push("administrator_staff_directory", "STAFF-100", entry(name="Mr. Bala Sani Jr", role="Senior Driver"),
                          operation="update", who=self.owner))
        self.assertEqual(self.stored("administrator_staff_directory", "STAFF-100").payload["role"], "Senior Driver")

    def test_the_administrator_reviews_the_file_but_cannot_change_who_someone_is(self):
        self.ok(self.add())
        admin = self.members["administrator"]
        self.ok(self.push("administrator_staff_directory", "STAFF-100", entry(fileStatus="Complete"),
                          operation="update", who=admin))
        self.assertEqual(self.stored("administrator_staff_directory", "STAFF-100").payload["fileStatus"], "Complete")
        for change in [dict(name="Someone Else"), dict(role="Principal"), dict(section="Finance")]:
            self.rejected(self.push("administrator_staff_directory", "STAFF-100", entry(fileStatus="Complete", **change),
                                    operation="update", who=admin), "not change who")

    def test_nobody_else_can_edit_and_nothing_is_deleted(self):
        self.ok(self.add())
        for role in ["principal", "accountant", "teacher", "staff", "parent"]:
            self.rejected(self.push("administrator_staff_directory", "STAFF-100", entry(fileStatus="Complete"),
                                    operation="update", who=self.members[role]))
        self.rejected(self.push("administrator_staff_directory", "STAFF-100", operation="delete", who=self.owner), "deleted")

    def test_server_owned_fields_cannot_be_set_or_changed_by_the_app(self):
        self.ok(self.add(systemRole="principal", staffCategory="approved", approvedFromProposal="P1", createdAt="2001"))
        stored = self.stored("administrator_staff_directory", "STAFF-100").payload
        self.assertNotIn("systemRole", stored)
        self.assertNotIn("approvedFromProposal", stored)
        self.assertEqual(stored["staffCategory"], "added_by_owner")
        # And an approved person's role cannot be changed by editing the entry.
        staff_id = self.make_staff(systemRole="teacher")
        current = dict(self.stored("administrator_staff_directory", staff_id).payload)
        self.ok(self.push("administrator_staff_directory", staff_id, {**current, "systemRole": "principal", "name": "Renamed"},
                          operation="update", who=self.owner))
        after = self.stored("administrator_staff_directory", staff_id).payload
        self.assertEqual((after["systemRole"], after["name"]), ("teacher", "Renamed"))

    def test_bad_entries_are_refused(self):
        for over in [dict(name=""), dict(role=""), dict(section=""), dict(fileStatus="Perfect"), dict(fileStatus="")]:
            self.rejected(self.add(**over))
        self.rejected(self.push("administrator_staff_directory", "STAFF-100", entry(id="STAFF-999"), who=self.owner), "id")

    def test_renaming_someone_updates_who_the_numbers_say_they_belong_to(self):
        staff_id = self.make_staff(name="Musa Ibrahim", phone="08031234567")
        current = dict(self.stored("administrator_staff_directory", staff_id).payload)
        self.ok(self.push("administrator_staff_directory", staff_id, {**current, "name": "Musa I. Yusuf"},
                          operation="update", who=self.owner))
        self.assertEqual(set(IdentityClaim.objects.values_list("holder_name", flat=True)), {"Musa I. Yusuf"})
        self.rejected(self.propose(phone="08031234567"), "already used by Musa I. Yusuf")
