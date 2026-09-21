from apps.access import catalog
from apps.schoollife.specs import calendar, campus, programmes

from .test_community import POST, post
from .test_modules import ModuleTestCase


class DriverTests(ModuleTestCase):
    def setUp(self):
        super().setUp()
        self.driver = self.members["driver"]

    def test_a_driver_has_their_own_screens_and_no_one_elses(self):
        keys = catalog.default_keys("driver")
        self.assertEqual({k for k in keys if k.startswith("driver.")}, {k for k in catalog.ACTIVITIES if k.startswith("driver.")})
        self.assertIn("driver.morning", keys)
        for other in ("teacher.students", "owner.payroll", "principal.teachers", "finance.payroll"):
            self.assertNotIn(other, keys)

    def test_a_driver_reads_transport_events_and_notices_but_not_boarding_or_visitors(self):
        for spec in (campus.TRANSPORT, calendar.EVENTS, campus.BOARDING, campus.VISITORS, programmes.TEACHING_MODELS):
            self.ok(self.send(spec, self.owner, "d1"))
        kinds = {t for t, _ in self.pulled(self.driver)}
        self.assertTrue({"school_transport_route", "school_event"} <= kinds)
        self.assertFalse(kinds & {"boarding_dorm", "visitor_record", "teaching_model_config"})

    def test_a_driver_can_post_to_the_community_and_report_a_found_item(self):
        self.ok(self.push(POST, "P-D", post("P-D"), who=self.driver))
        self.ok(self.push("lost_found_item", "L-D", {"id": "L-D", "item": "Water bottle"}, who=self.driver))

    def test_a_driver_cannot_write_to_other_modules(self):
        self.rejected(self.send(calendar.EVENTS, self.driver, "x1"), "role may not")
        self.rejected(self.send(campus.TRANSPORT, self.driver, "x2"), "role may not")

    def test_a_driver_can_be_proposed_and_approved_as_staff(self):
        staff_id = self.make_staff(systemRole="driver", email="driver@school.ng")
        self.assertEqual(self.profile(staff_id)["systemRole"], "driver")
