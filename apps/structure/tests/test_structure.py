from apps.staff.tests.helpers import StaffTestCase
from apps.sync.models import SyncRecord

SECTION = "academic_section"
POST = "leadership_appointment"
LOOK = "school_appearance"
PNG_MAGIC = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])


def section(id="primary", **over):
    body = {"id": id, "name": "Primary School", "stage": "Primary", "campus": "Kaduna Campus",
            "leaderTitle": "Headmistress", "leaderName": "Mrs. Hauwa Sule", "classes": 6}
    body.update(over)
    return body


def post(id="L-001", **over):
    body = {"id": id, "person": "Mrs. Hauwa Sule", "title": "Headmistress", "level": "sectionHead",
            "sectionId": "primary", "department": None, "reportsTo": None}
    body.update(over)
    return body


class StructureTestCase(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.ok(self.push(SECTION, "primary", section()))
        self.ok(self.push(SECTION, "secondary", section("secondary", name="Secondary School", stage="Secondary")))
        self.ok(self.push(POST, "L-001", post()))

    def add(self, id, **over):
        body = post(id, level="deputy", title="Deputy", person="Mr. Deputy", reportsTo="L-001")
        return self.push(POST, id, {**body, **over})


class SectionTests(StructureTestCase):
    def test_only_the_owner_changes_the_structure(self):
        for role in ("principal", "administrator", "accountant", "teacher", "staff", "parent", "student"):
            self.rejected(self.push(SECTION, "x", section("x"), who=self.members[role]))
        self.assertEqual(self.push(SECTION, "x", section("x"), who=self.other_owner, school=self.school).status_code, 403)

    def test_it_is_cleaned_and_stamped_by_the_server(self):
        stored = self.stored(SECTION, "primary").payload
        self.assertEqual(stored["updatedByMembershipId"], str(self.owner.id))
        self.assertTrue(stored["updatedAt"])
        self.ok(self.push(SECTION, "primary", section(classes=7, evil="x", updatedByMembershipId="fake"), operation="update"))
        stored = self.stored(SECTION, "primary").payload
        self.assertEqual((stored["classes"], "evil" in stored, stored["updatedByMembershipId"]), (7, False, str(self.owner.id)))

    def test_bad_sections_are_refused(self):
        for over in ({"id": "other"}, {"name": ""}, {"stage": ""}, {"campus": ""}, {"classes": -1}, {"classes": "6"},
                     {"classes": 501}, {"name": "x" * 121}):
            self.rejected(self.push(SECTION, "bad", {**section("bad"), **over}))

    def test_a_section_cannot_be_deleted(self):
        self.rejected(self.push(SECTION, "primary", operation="delete"))

    def test_a_conflicting_edit_is_reported(self):
        response = self.push(SECTION, "primary", section(), operation="update", base_version=99)
        self.assertEqual(response.status_code, 409)


class AppointmentTests(StructureTestCase):
    def test_a_section_has_one_head(self):
        self.rejected(self.push(POST, "L-002", post("L-002", person="Someone Else")), "already has a Section Head")
        self.ok(self.push(POST, "L-010", post("L-010", sectionId="secondary", person="Mr. Danladi", title="Principal")))

    def test_the_head_can_be_replaced_by_changing_the_person(self):
        self.ok(self.push(POST, "L-001", post(person="Mrs. New Head"), operation="update"))
        self.assertEqual(self.stored(POST, "L-001").payload["person"], "Mrs. New Head")

    def test_the_section_must_exist(self):
        self.rejected(self.push(POST, "L-002", post("L-002", sectionId="ghost")), "does not exist")
        self.rejected(self.push(POST, "L-003", post("L-003", sectionId="")))

    def test_the_section_must_be_this_schools_own(self):
        self.ok(self.push(SECTION, "primary", section(), who=self.other_owner, school=self.other_school))
        self.ok(self.push(POST, "L-001", post(), who=self.other_owner, school=self.other_school))
        self.assertEqual(self.stored(POST, "L-001", school=self.other_school).payload["person"], "Mrs. Hauwa Sule")
        self.rejected(self.push(POST, "L-020", post("L-020", sectionId="nowhere"), who=self.other_owner, school=self.other_school))

    def test_others_report_to_a_head_or_deputy_of_the_same_section(self):
        self.ok(self.add("L-002"))
        self.ok(self.push(POST, "L-003", post("L-003", level="coordinator", title="Coordinator", person="Ms C", reportsTo="L-002")))
        self.rejected(self.add("L-004", reportsTo="L-999"), "reporting manager")
        self.rejected(self.push(POST, "L-005", post("L-005", level="deputy", title="D", person="X", reportsTo="")), "reportsTo is required")

    def test_someone_who_is_not_a_head_or_deputy_cannot_be_a_manager(self):
        self.ok(self.push(POST, "L-003", post("L-003", level="coordinator", title="C", person="Ms C", reportsTo="L-001")))
        self.rejected(self.push(POST, "L-004", post("L-004", level="coordinator", title="C", person="Mr D", reportsTo="L-003")),
                      "reporting manager")

    def test_a_manager_from_another_section_is_refused(self):
        self.ok(self.push(POST, "L-010", post("L-010", sectionId="secondary", person="P", title="Principal")))
        self.rejected(self.add("L-011", reportsTo="L-010"), "same section")

    def test_a_head_of_department_needs_a_department(self):
        hod = dict(level="hod", title="HOD", person="Mr. H", reportsTo="L-001")
        self.rejected(self.push(POST, "L-006", post("L-006", **hod)), "department")
        self.ok(self.push(POST, "L-006", post("L-006", department="Mathematics", **hod)))
        self.assertEqual(self.stored(POST, "L-006").payload["department"], "Mathematics")

    def test_a_department_is_kept_only_for_heads_of_department(self):
        self.ok(self.add("L-002", department="Whatever"))
        self.assertIsNone(self.stored(POST, "L-002").payload["department"])

    def test_a_head_reports_to_no_one(self):
        self.ok(self.push(POST, "L-010", post("L-010", sectionId="secondary", person="P", title="Principal", reportsTo="L-001")))
        self.assertIsNone(self.stored(POST, "L-010").payload["reportsTo"])

    def test_nobody_reports_in_a_circle(self):
        self.ok(self.add("L-002"))
        self.ok(self.add("L-003", reportsTo="L-002"))
        self.rejected(self.push(POST, "L-002", {**self.stored(POST, "L-002").payload, "reportsTo": "L-003"},
                                operation="update"), "itself")
        self.rejected(self.push(POST, "L-003", {**self.stored(POST, "L-003").payload, "reportsTo": "L-003"},
                                operation="update"), "reporting manager")

    def test_a_posts_section_and_level_are_fixed(self):
        self.ok(self.add("L-002"))
        stored = self.stored(POST, "L-002").payload
        self.rejected(self.push(POST, "L-002", {**stored, "level": "coordinator"}, operation="update"), "cannot change")
        self.rejected(self.push(POST, "L-002", {**stored, "sectionId": "secondary"}, operation="update"), "cannot change")

    def test_bad_values_and_forged_fields_are_refused_or_ignored(self):
        for over in ({"level": "king"}, {"person": ""}, {"title": ""}, {"id": "other"}):
            body = {**post("L-030", level="deputy", reportsTo="L-001", person="P", title="T"), **over}
            self.rejected(self.push(POST, "L-030", body))
        self.ok(self.add("L-031", updatedByMembershipId="fake", extra="x"))
        stored = self.stored(POST, "L-031").payload
        self.assertEqual((stored["updatedByMembershipId"], "extra" in stored), (str(self.owner.id), False))

    def test_only_the_owner_appoints_and_nothing_is_deleted(self):
        for role in ("principal", "administrator", "teacher", "parent"):
            self.rejected(self.push(POST, "L-040", post("L-040", level="deputy", reportsTo="L-001"), who=self.members[role]))
        self.rejected(self.push(POST, "L-001", operation="delete"))

    def test_a_refused_appointment_leaves_nothing_behind(self):
        self.rejected(self.push(POST, "L-002", post("L-002")))
        self.assertFalse(SyncRecord.objects.filter(entity_type=POST, entity_id="L-002").exists())


class AppearanceTests(StructureTestCase):
    def test_the_owner_chooses_a_colour_scheme(self):
        self.ok(self.push(LOOK, "theme", {"themeId": "ocean"}))
        stored = self.stored(LOOK, "theme").payload
        self.assertEqual((stored["themeId"], stored["updatedByMembershipId"]), ("ocean", str(self.owner.id)))
        self.ok(self.push(LOOK, "theme", {"themeId": "rose"}, operation="update"))
        self.assertEqual(self.stored(LOOK, "theme").payload["themeId"], "rose")

    def test_the_owner_can_use_any_ready_made_scheme(self):
        from apps.structure.constants import THEMES

        for number, theme in enumerate(sorted(THEMES)):
            self.ok(self.push(LOOK, "theme", {"themeId": theme}, operation="update" if number else "create"))

    def test_the_owner_can_choose_their_own_colours(self):
        custom = {"themeId": "custom", "primaryArgb": 0xFF112233, "accentArgb": 0xFFEEDDCC}
        self.ok(self.push(LOOK, "theme", custom))
        stored = self.stored(LOOK, "theme").payload
        self.assertEqual((stored["primaryArgb"], stored["accentArgb"]), (0xFF112233, 0xFFEEDDCC))

    def test_own_colours_must_be_real_colours(self):
        self.rejected(self.push(LOOK, "theme", {"themeId": "custom"}))
        self.rejected(self.push(LOOK, "theme", {"themeId": "custom", "primaryArgb": "red", "accentArgb": 1}))
        self.rejected(self.push(LOOK, "theme", {"themeId": "custom", "primaryArgb": 5, "accentArgb": 0xFFEEDDCC}))

    def test_the_owner_can_set_a_logo_and_only_a_real_small_picture(self):
        import base64

        png = base64.b64encode(PNG_MAGIC + b"0" * 50).decode()
        self.ok(self.push(LOOK, "theme", {"themeId": "ocean", "logo": png}))
        self.assertEqual(self.stored(LOOK, "theme").payload["logo"], png)
        self.rejected(self.push(LOOK, "theme", {"themeId": "ocean", "logo": "not base64!!"}, operation="update"))
        text = base64.b64encode(b"just some text").decode()
        self.rejected(self.push(LOOK, "theme", {"themeId": "ocean", "logo": text}, operation="update"))
        huge = base64.b64encode(PNG_MAGIC + b"0" * 160_000).decode()
        self.rejected(self.push(LOOK, "theme", {"themeId": "ocean", "logo": huge}, operation="update"))

    def test_only_the_owner_may_and_only_real_schemes_and_one_record(self):
        for role in ("principal", "administrator", "teacher", "parent"):
            self.rejected(self.push(LOOK, "theme", {"themeId": "ocean"}, who=self.members[role]))
        self.rejected(self.push(LOOK, "theme", {"themeId": "neon"}))
        self.rejected(self.push(LOOK, "another", {"themeId": "ocean"}))


class WhoReadsItTests(StructureTestCase):
    def setUp(self):
        super().setUp()
        self.ok(self.push(LOOK, "theme", {"themeId": "ocean"}))

    def kinds(self, who):
        self.client.force_authenticate(who.user)
        found = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id)}).json()["records"]
        return {r["entityType"] for r in found}

    def test_everyone_receives_the_sections_and_the_look(self):
        for role in ("proprietor", "principal", "administrator", "accountant", "teacher", "staff", "parent", "student"):
            self.assertGreaterEqual(self.kinds(self.members[role]) & {SECTION, LOOK}, {SECTION, LOOK}, role)

    def test_appointments_go_to_the_people_who_work_there(self):
        for role in ("proprietor", "principal", "administrator", "accountant", "teacher", "staff"):
            self.assertIn(POST, self.kinds(self.members[role]), role)
        for role in ("parent", "student"):
            self.assertNotIn(POST, self.kinds(self.members[role]), role)
