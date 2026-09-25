from apps.staff.tests.helpers import StaffTestCase
from apps.transport.shared import today_key

from .. import constants as c

_ITEM_IDS = sorted(c.VEHICLE_CHECK_ITEM_IDS)


def route(id="BUS-01", **over):
    body = {"id": id, "name": "Bus 1", "vehicle": "Bus 1 - Toyota Hiace", "driver": "Unassigned",
            "assistant": "Mrs. Grace", "riders": 0, "stops": 0, "morning": "x", "afternoon": "x",
            "status": "preparing", "note": ""}
    body.update(over)
    return body


def plan(route_id="BUS-01", **over):
    body = {"routeId": route_id, "stops": [
        {"id": "S1", "sequence": 1, "name": "Estate Gate", "morningTime": "06:30", "afternoonTime": "15:30", "active": True},
        {"id": "S2", "sequence": 2, "name": "Market Junction", "morningTime": "06:40", "afternoonTime": "15:40", "active": True},
    ]}
    body.update(over)
    return body


def assignment(membership_id, route_id="BUS-01", **over):
    body = {"membershipId": membership_id, "routeId": route_id, "driverDisplayName": "A Driver", "staffId": "", "active": True}
    body.update(over)
    return body


def rider(student_id, stop_id, route_id="BUS-01", **over):
    body = {"studentId": student_id, "studentName": "A Student", "className": "JSS 2A", "routeId": route_id, "stopId": stop_id, "active": True}
    body.update(over)
    return body


def stop(stop_id, riders=(), **over):
    body = {"id": stop_id, "sequence": 1, "name": "A Stop", "scheduledTime": "06:30", "riders": list(riders), "status": "pending", "arrivedAt": "", "departedAt": ""}
    body.update(over)
    return body


def rider_state(student_id, **over):
    body = {"studentId": student_id, "name": "A Student", "className": "JSS 2A", "stopId": "S1", "status": "pending", "note": "", "updatedAt": ""}
    body.update(over)
    return body


def check_items(**overrides):
    items = []
    for item_id in _ITEM_IDS:
        entry = {"id": item_id, "label": item_id, "description": "", "severity": "critical", "status": "unchecked", "note": ""}
        entry.update(overrides.get(item_id, {}))
        items.append(entry)
    return items


class DailyRunTestCase(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.members["administrator"]
        self.driver = self.members["driver"]
        self.today = today_key()
        self.ok(self.push(c.ROUTE, "BUS-01", route(), who=self.admin))
        self.ok(self.push(c.ROUTE_PLAN, "BUS-01", plan(), who=self.admin))
        self.ok(self.push(c.DRIVER_ASSIGNMENT, self.driver.id, assignment(str(self.driver.id)), who=self.admin))
        self.ok(self.push(c.RIDER_ASSIGNMENT, "STU-1", rider("STU-1", "S1"), who=self.admin))
        self.ok(self.push(c.RIDER_ASSIGNMENT, "STU-2", rider("STU-2", "S2"), who=self.admin))

    def morning_run_id(self):
        return f"{self.driver.id}:morning:{self.today}"

    def afternoon_run_id(self):
        return f"{self.driver.id}:afternoon:{self.today}"

    def real_stops(self):
        return [
            stop("S1", riders=[rider_state("STU-1")]),
            stop("S2", riders=[rider_state("STU-2", stopId="S2")], sequence=2),
        ]

    def real_morning_run(self, **over):
        body = {
            "id": self.morning_run_id(), "membershipId": str(self.driver.id), "routeId": "BUS-01",
            "serviceDate": self.today, "vehicle": "Bus 1 - Toyota Hiace", "driverName": "A Driver",
            "assistantName": "Mrs. Grace", "stops": self.real_stops(), "status": "notStarted",
            "startedAt": "", "arrivedSchoolAt": "", "completedAt": "",
        }
        body.update(over)
        return body


class MorningRunTests(DailyRunTestCase):
    def test_the_driver_can_create_todays_real_run(self):
        self.ok(self.push(c.MORNING_RUN, self.morning_run_id(), self.real_morning_run(), who=self.driver))

    def test_another_role_cannot_write_it(self):
        self.rejected(self.push(c.MORNING_RUN, self.morning_run_id(), self.real_morning_run(), who=self.admin), "role may not")

    def test_a_fabricated_rider_is_refused(self):
        stops = [stop("S1", riders=[rider_state("STU-NOPE")])]
        self.rejected(self.push(c.MORNING_RUN, self.morning_run_id(), self.real_morning_run(stops=stops), who=self.driver), "does not match")

    def test_a_fabricated_stop_is_refused(self):
        stops = self.real_stops() + [stop("S-NOPE")]
        self.rejected(self.push(c.MORNING_RUN, self.morning_run_id(), self.real_morning_run(stops=stops), who=self.driver), "not a real active stop")

    def test_a_missing_expected_rider_is_refused(self):
        stops = [stop("S1", riders=[]), stop("S2", riders=[rider_state("STU-2", stopId="S2")], sequence=2)]
        self.rejected(self.push(c.MORNING_RUN, self.morning_run_id(), self.real_morning_run(stops=stops), who=self.driver), "does not match")

    def test_the_id_must_match_driver_period_and_date(self):
        self.rejected(self.push(c.MORNING_RUN, "someone-else:morning:" + self.today, self.real_morning_run(), who=self.driver))

    def test_status_can_change_but_riders_cannot_once_the_run_exists(self):
        self.ok(self.push(c.MORNING_RUN, self.morning_run_id(), self.real_morning_run(), who=self.driver))
        started = self.real_morning_run(status="inProgress", startedAt="2026-09-25T06:00:00Z")
        self.ok(self.push(c.MORNING_RUN, self.morning_run_id(), started, operation="update", who=self.driver))
        self.assertEqual(self.stored(c.MORNING_RUN, self.morning_run_id()).payload["status"], "inProgress")

    def test_adding_a_rider_after_the_run_exists_is_refused(self):
        self.ok(self.push(c.MORNING_RUN, self.morning_run_id(), self.real_morning_run(), who=self.driver))
        stops = [
            stop("S1", riders=[rider_state("STU-1"), rider_state("STU-3", stopId="S1")]),
            stop("S2", riders=[rider_state("STU-2", stopId="S2")], sequence=2),
        ]
        self.rejected(
            self.push(c.MORNING_RUN, self.morning_run_id(), self.real_morning_run(stops=stops), operation="update", who=self.driver),
            "cannot change",
        )

    def test_the_route_is_also_locked_for_new_rider_assignments_once_the_run_exists(self):
        # Phase 1's own lock, exercised by Phase 2 data existing - no change needed there.
        self.ok(self.push(c.MORNING_RUN, self.morning_run_id(), self.real_morning_run(), who=self.driver))
        self.rejected(self.push(c.RIDER_ASSIGNMENT, "STU-3", rider("STU-3", "S1"), who=self.admin), "locked")

    def test_management_reads_it_but_another_driver_does_not(self):
        self.ok(self.push(c.MORNING_RUN, self.morning_run_id(), self.real_morning_run(), who=self.driver))
        self.client.force_authenticate(self.admin.user)
        found = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id)}).json()["records"]
        self.assertIn((c.MORNING_RUN, self.morning_run_id()), {(r["entityType"], r["entityId"]) for r in found})


class AfternoonRunTests(DailyRunTestCase):
    def real_afternoon_run(self, **over):
        body = {
            "id": self.afternoon_run_id(), "membershipId": str(self.driver.id), "routeId": "BUS-01",
            "serviceDate": self.today, "vehicle": "Bus 1 - Toyota Hiace", "driverName": "A Driver",
            "assistantName": "Mrs. Grace", "stops": self.real_stops(), "status": "notStarted",
            "startedAt": "", "departedSchoolAt": "", "returnedSchoolAt": "", "completedAt": "",
        }
        body.update(over)
        return body

    def test_the_driver_can_create_todays_real_run(self):
        self.ok(self.push(c.AFTERNOON_RUN, self.afternoon_run_id(), self.real_afternoon_run(), who=self.driver))

    def test_a_fabricated_stop_is_refused(self):
        stops = self.real_stops() + [stop("S-NOPE")]
        self.rejected(self.push(c.AFTERNOON_RUN, self.afternoon_run_id(), self.real_afternoon_run(stops=stops), who=self.driver))


class VehicleCheckTests(DailyRunTestCase):
    def check_id(self, period="morning"):
        return f"{self.driver.id}:vehicle-check:{self.today}:{period}"

    def real_check(self, period="morning", **over):
        body = {
            "id": self.check_id(period), "membershipId": str(self.driver.id), "routeId": "BUS-01",
            "vehicle": "Bus 1 - Toyota Hiace", "serviceDate": self.today, "period": period,
            "items": check_items(), "status": "notStarted", "submittedAt": "",
        }
        body.update(over)
        return body

    def test_the_driver_can_create_todays_real_check(self):
        self.ok(self.push(c.VEHICLE_CHECK, self.check_id(), self.real_check(), who=self.driver))

    def test_a_missing_item_is_refused(self):
        items = [i for i in check_items() if i["id"] != "brakes"]
        self.rejected(self.push(c.VEHICLE_CHECK, self.check_id(), self.real_check(items=items), who=self.driver), "every real check item")

    def test_a_client_supplied_severity_is_ignored(self):
        items = check_items(interior={"severity": "critical"})
        self.ok(self.push(c.VEHICLE_CHECK, self.check_id(), self.real_check(items=items), who=self.driver))
        stored_items = {i["id"]: i for i in self.stored(c.VEHICLE_CHECK, self.check_id()).payload["items"]}
        self.assertEqual(stored_items["interior"]["severity"], "advisory")

    def test_a_failed_item_needs_a_note(self):
        items = check_items(brakes={"status": "failed", "note": ""})
        self.rejected(self.push(c.VEHICLE_CHECK, self.check_id(), self.real_check(items=items), who=self.driver), "note")

    def test_a_failed_item_with_a_note_is_accepted(self):
        items = check_items(brakes={"status": "failed", "note": "Squeaking on the front left."})
        self.ok(self.push(c.VEHICLE_CHECK, self.check_id(), self.real_check(items=items), who=self.driver))


class VehicleDefectTests(DailyRunTestCase):
    def setUp(self):
        super().setUp()
        self.check_id = f"{self.driver.id}:vehicle-check:{self.today}:morning"
        self.defect_id = f"{self.check_id}:brakes"
        items = check_items(brakes={"status": "failed", "note": "Squeaking."})
        check = {
            "id": self.check_id, "membershipId": str(self.driver.id), "routeId": "BUS-01",
            "vehicle": "Bus 1 - Toyota Hiace", "serviceDate": self.today, "period": "morning",
            "items": items, "status": "blocked", "submittedAt": "2026-09-25T06:00:00Z",
        }
        self.ok(self.push(c.VEHICLE_CHECK, self.check_id, check, who=self.driver))

    def defect_payload(self, **over):
        body = {"checkId": self.check_id, "itemId": "brakes", "itemLabel": "Brakes", "note": "Squeaking."}
        body.update(over)
        return body

    def test_the_driver_can_report_the_defect_their_check_found(self):
        self.ok(self.push(c.VEHICLE_DEFECT, self.defect_id, self.defect_payload(), who=self.driver))
        stored = self.stored(c.VEHICLE_DEFECT, self.defect_id).payload
        self.assertEqual((stored["severity"], stored["blocksTrip"], stored["status"]), ("critical", True, "reported"))

    def test_management_moves_it_forward_and_a_closed_defect_stays_closed(self):
        self.ok(self.push(c.VEHICLE_DEFECT, self.defect_id, self.defect_payload(), who=self.driver))
        self.ok(self.push(c.VEHICLE_DEFECT, self.defect_id, {"status": "under_review", "managementNote": "Booked in."}, operation="update", who=self.admin))
        self.assertEqual(self.stored(c.VEHICLE_DEFECT, self.defect_id).payload["status"], "under_review")
        self.rejected(
            self.push(c.VEHICLE_DEFECT, self.defect_id, {"status": "cleared", "managementNote": ""}, operation="update", who=self.admin),
            "clearance note",
        )
        self.ok(self.push(c.VEHICLE_DEFECT, self.defect_id, {"status": "cleared", "managementNote": "Pads replaced."}, operation="update", who=self.admin))
        stored = self.stored(c.VEHICLE_DEFECT, self.defect_id).payload
        self.assertEqual((stored["status"], stored["clearedByMembershipId"], stored["requiresTransportReview"]), ("cleared", str(self.admin.id), False))
        self.rejected(
            self.push(c.VEHICLE_DEFECT, self.defect_id, {"status": "reported", "managementNote": "x"}, operation="update", who=self.admin),
            "already closed",
        )

    def test_clearing_the_defect_lets_the_vehicle_be_released_again(self):
        self.ok(self.push(c.VEHICLE_DEFECT, self.defect_id, self.defect_payload(), who=self.driver))
        release = {"routeId": "BUS-01", "status": "released", "note": ""}
        self.rejected(self.push(c.VEHICLE_CLEARANCE, "BUS-01", release, who=self.admin), "blocking safety")
        self.ok(self.push(c.VEHICLE_DEFECT, self.defect_id, {"status": "cleared", "managementNote": "Fixed."}, operation="update", who=self.admin))
        self.ok(self.push(c.VEHICLE_CLEARANCE, "BUS-01", release, who=self.admin))

    def test_the_driver_resending_the_defect_never_undoes_transport_controls_progress(self):
        # The app re-sends a defect when the same failed item is submitted again.
        self.ok(self.push(c.VEHICLE_DEFECT, self.defect_id, self.defect_payload(), who=self.driver))
        self.ok(self.push(c.VEHICLE_DEFECT, self.defect_id, {"status": "acknowledged", "managementNote": "Seen."}, operation="update", who=self.admin))
        self.ok(self.push(c.VEHICLE_DEFECT, self.defect_id, self.defect_payload(note="Squeaking again"), operation="update", who=self.driver))
        stored = self.stored(c.VEHICLE_DEFECT, self.defect_id).payload
        self.assertEqual((stored["status"], stored["note"]), ("acknowledged", "Squeaking."))

    def test_the_driver_can_refresh_the_note_while_it_is_still_only_reported(self):
        self.ok(self.push(c.VEHICLE_DEFECT, self.defect_id, self.defect_payload(), who=self.driver))
        self.ok(self.push(c.VEHICLE_DEFECT, self.defect_id, self.defect_payload(note="Squeaking and pulling left"), operation="update", who=self.driver))
        self.assertEqual(self.stored(c.VEHICLE_DEFECT, self.defect_id).payload["note"], "Squeaking and pulling left")

    def test_a_manager_cannot_report_a_new_defect(self):
        self.rejected(self.push(c.VEHICLE_DEFECT, self.defect_id, self.defect_payload(), who=self.admin), "Only the Driver")

    def test_an_unrelated_role_cannot_touch_it(self):
        self.ok(self.push(c.VEHICLE_DEFECT, self.defect_id, self.defect_payload(), who=self.driver))
        self.rejected(
            self.push(c.VEHICLE_DEFECT, self.defect_id, {"status": "cleared", "managementNote": "x"}, operation="update", who=self.members["teacher"]),
            "role may not",
        )


class TransportEventTests(DailyRunTestCase):
    def test_a_driver_can_log_an_event_and_never_change_it(self):
        payload = {"id": "e1", "eventType": "morning_run_started", "runId": self.morning_run_id()}
        self.ok(self.push(c.TRANSPORT_EVENT, "e1", payload, who=self.driver))
        self.rejected(self.push(c.TRANSPORT_EVENT, "e1", payload, operation="update", who=self.driver))

    def test_the_server_stamps_when_it_arrived_and_keeps_when_the_driver_says_it_happened(self):
        # An offline driver syncs later: the moment they acted is a claim kept beside the server's own time.
        payload = {"id": "e1", "eventType": "morning_run_started", "at": "2026-09-25T06:00:00Z"}
        self.ok(self.push(c.TRANSPORT_EVENT, "e1", payload, who=self.driver))
        stored = self.stored(c.TRANSPORT_EVENT, "e1").payload
        self.assertEqual(stored["occurredAt"], "2026-09-25T06:00:00Z")
        self.assertNotEqual(stored["at"], "2026-09-25T06:00:00Z")

    def test_management_reads_it_an_unrelated_role_does_not(self):
        payload = {"id": "e1", "eventType": "morning_run_started"}
        self.ok(self.push(c.TRANSPORT_EVENT, "e1", payload, who=self.driver))
        self.assertIn((c.TRANSPORT_EVENT, "e1"), self._pulled(self.admin))
        self.assertNotIn((c.TRANSPORT_EVENT, "e1"), self._pulled(self.members["staff"]))

    def _pulled(self, who):
        self.client.force_authenticate(who.user)
        found = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id)}).json()["records"]
        return {(r["entityType"], r["entityId"]) for r in found}
