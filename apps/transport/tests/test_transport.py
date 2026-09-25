from apps.staff.tests.helpers import StaffTestCase
from apps.sync.models import SyncRecord

from .. import constants as c


def route(id="BUS-01", **over):
    body = {
        "id": id, "name": "Bus 1", "vehicle": "Bus 1 - Toyota Hiace", "driver": "Unassigned",
        "assistant": "Mrs. Grace", "riders": 0, "stops": 0, "morning": "Not configured",
        "afternoon": "Not configured", "status": "preparing", "note": "",
    }
    body.update(over)
    return body


def plan(route_id="BUS-01", stops=None, **over):
    body = {
        "routeId": route_id,
        "stops": stops if stops is not None else [
            {"id": "S1", "sequence": 1, "name": "Estate Gate", "morningTime": "06:30", "afternoonTime": "15:30", "active": True},
        ],
    }
    body.update(over)
    return body


def assignment(membership_id, route_id="BUS-01", **over):
    body = {"membershipId": membership_id, "routeId": route_id, "driverDisplayName": "A Driver", "staffId": "", "active": True}
    body.update(over)
    return body


def rider(student_id="STU-1", route_id="BUS-01", stop_id="S1", **over):
    body = {"studentId": student_id, "studentName": "Maryam Abdullahi", "className": "JSS 2A", "routeId": route_id, "stopId": stop_id, "active": True}
    body.update(over)
    return body


def clearance(route_id="BUS-01", status="released", **over):
    body = {"routeId": route_id, "status": status, "note": ""}
    body.update(over)
    return body


class TransportTestCase(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.members["administrator"]
        self.driver = self.members["driver"]
        self.principal = self.members["principal"]
        self.ok(self.push(c.ROUTE, "BUS-01", route(), who=self.admin))
        self.ok(self.push(c.ROUTE, "BUS-02", route("BUS-02", name="Bus 2"), who=self.admin))
        self.ok(self.push(c.ROUTE_PLAN, "BUS-01", plan(), who=self.admin))

    def service_record(self, entity_type, entity_id, payload):
        """A record for an entity type that belongs to a later phase (a run, a check, a
        defect) - created directly, the way a future phase's handler would store it."""
        SyncRecord.objects.create(school=self.school, entity_type=entity_type, entity_id=entity_id, payload=payload)


class DriverAssignmentTests(TransportTestCase):
    def test_administrator_and_proprietor_can_assign_a_driver(self):
        self.ok(self.push(c.DRIVER_ASSIGNMENT, self.driver.id, assignment(str(self.driver.id)), who=self.admin))
        other = self.members["staff"]
        self.ok(self.push(c.DRIVER_ASSIGNMENT, other.id, assignment(str(other.id), route_id="BUS-02"), who=self.owner))

    def test_principal_and_driver_cannot_assign(self):
        for who in (self.principal, self.driver):
            self.rejected(self.push(c.DRIVER_ASSIGNMENT, self.driver.id, assignment(str(self.driver.id)), who=who), "role may not")

    def test_a_route_cannot_have_two_active_drivers(self):
        self.ok(self.push(c.DRIVER_ASSIGNMENT, self.driver.id, assignment(str(self.driver.id)), who=self.admin))
        other = self.members["staff"]
        self.rejected(
            self.push(c.DRIVER_ASSIGNMENT, other.id, assignment(str(other.id)), who=self.admin),
            "already assigned",
        )

    def test_assigning_to_an_unknown_route_is_refused(self):
        self.rejected(
            self.push(c.DRIVER_ASSIGNMENT, self.driver.id, assignment(str(self.driver.id), route_id="BUS-99"), who=self.admin),
            "does not exist",
        )

    def test_unassigning_is_a_real_update(self):
        self.ok(self.push(c.DRIVER_ASSIGNMENT, self.driver.id, assignment(str(self.driver.id)), who=self.admin))
        self.ok(self.push(c.DRIVER_ASSIGNMENT, self.driver.id, assignment(str(self.driver.id), route_id="", active=False), operation="update", who=self.admin))
        self.assertFalse(self.stored(c.DRIVER_ASSIGNMENT, self.driver.id).payload["active"])

    def test_a_driver_already_in_service_today_cannot_be_reassigned(self):
        self.ok(self.push(c.DRIVER_ASSIGNMENT, self.driver.id, assignment(str(self.driver.id)), who=self.admin))
        today = self._today()
        self.service_record(c.MORNING_RUN, f"{self.driver.id}:morning:{today}", {"routeId": "BUS-01", "serviceDate": today})
        self.rejected(
            self.push(c.DRIVER_ASSIGNMENT, self.driver.id, assignment(str(self.driver.id), route_id="BUS-02"), operation="update", who=self.admin),
            "locked",
        )

    def test_a_route_already_in_service_today_cannot_take_a_new_driver(self):
        today = self._today()
        self.service_record(c.MORNING_RUN, f"someone-else:morning:{today}", {"routeId": "BUS-01", "serviceDate": today})
        self.rejected(
            self.push(c.DRIVER_ASSIGNMENT, self.driver.id, assignment(str(self.driver.id)), who=self.admin),
            "already has transport preparation",
        )

    def test_a_driver_reads_only_their_own_assignment(self):
        other = self.members["staff"]
        self.ok(self.push(c.DRIVER_ASSIGNMENT, self.driver.id, assignment(str(self.driver.id)), who=self.admin))
        self.ok(self.push(c.DRIVER_ASSIGNMENT, other.id, assignment(str(other.id), route_id="BUS-02"), who=self.admin))
        seen = {i for t, i in self._pulled(self.driver) if t == c.DRIVER_ASSIGNMENT}
        self.assertEqual(seen, {str(self.driver.id)})

    def test_management_reads_every_assignment(self):
        self.ok(self.push(c.DRIVER_ASSIGNMENT, self.driver.id, assignment(str(self.driver.id)), who=self.admin))
        for who in (self.owner, self.admin, self.principal):
            self.assertIn((c.DRIVER_ASSIGNMENT, str(self.driver.id)), self._pulled(who))

    def _today(self):
        from apps.transport.shared import today_key

        return today_key()

    def _pulled(self, who):
        self.client.force_authenticate(who.user)
        found = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id)}).json()["records"]
        return {(r["entityType"], r["entityId"]) for r in found}


class AssignmentEventTests(TransportTestCase):
    def test_an_event_can_be_created_but_never_changed(self):
        payload = {"id": "e1", "eventType": "driver_route_assigned", "driverMembershipId": str(self.driver.id), "newRouteId": "BUS-01"}
        self.ok(self.push(c.ASSIGNMENT_EVENT, "e1", payload, who=self.admin))
        self.rejected(self.push(c.ASSIGNMENT_EVENT, "e1", payload, operation="update", who=self.admin))

    def test_a_driver_cannot_add_an_event(self):
        payload = {"id": "e1", "eventType": "driver_route_assigned", "driverMembershipId": str(self.driver.id)}
        self.rejected(self.push(c.ASSIGNMENT_EVENT, "e1", payload, who=self.driver), "role may not")


class RiderAssignmentTests(TransportTestCase):
    def test_a_student_can_be_assigned_to_a_real_active_stop(self):
        self.ok(self.push(c.RIDER_ASSIGNMENT, "STU-1", rider(), who=self.admin))
        self.assertEqual(self.stored(c.ROUTE, "BUS-01").payload["riders"], 1)

    def test_assigning_to_an_inactive_or_unknown_stop_is_refused(self):
        self.rejected(self.push(c.RIDER_ASSIGNMENT, "STU-1", rider(stop_id="S-NOPE"), who=self.admin), "not active")

    def test_a_driver_reads_only_riders_on_their_own_route(self):
        self.ok(self.push(c.ROUTE_PLAN, "BUS-02", plan("BUS-02", stops=[
            {"id": "S2", "sequence": 1, "name": "Other Gate", "morningTime": "06:35", "afternoonTime": "15:35", "active": True},
        ]), who=self.admin))
        self.ok(self.push(c.DRIVER_ASSIGNMENT, self.driver.id, assignment(str(self.driver.id), route_id="BUS-01"), who=self.admin))
        self.ok(self.push(c.RIDER_ASSIGNMENT, "STU-1", rider(route_id="BUS-01"), who=self.admin))
        self.ok(self.push(c.RIDER_ASSIGNMENT, "STU-2", rider("STU-2", route_id="BUS-02", stop_id="S2"), who=self.admin))

        self.client.force_authenticate(self.driver.user)
        found = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id)}).json()["records"]
        seen = {r["entityId"] for r in found if r["entityType"] == c.RIDER_ASSIGNMENT}
        self.assertEqual(seen, {"STU-1"})

    def test_removing_a_student_frees_the_stop_and_updates_the_route_count(self):
        self.ok(self.push(c.RIDER_ASSIGNMENT, "STU-1", rider(), who=self.admin))
        self.ok(self.push(c.RIDER_ASSIGNMENT, "STU-1", rider(route_id="", stop_id="", active=False), operation="update", who=self.admin))
        self.assertEqual(self.stored(c.ROUTE, "BUS-01").payload["riders"], 0)

    def test_a_locked_route_refuses_a_new_rider(self):
        today = self._today()
        self.service_record(c.MORNING_RUN, f"x:morning:{today}", {"routeId": "BUS-01", "serviceDate": today})
        self.rejected(self.push(c.RIDER_ASSIGNMENT, "STU-1", rider(), who=self.admin), "locked")

    def _today(self):
        from apps.transport.shared import today_key

        return today_key()


class RoutePlanTests(TransportTestCase):
    def test_duplicate_active_stop_names_are_refused(self):
        stops = [
            {"id": "S1", "sequence": 1, "name": "Estate Gate", "morningTime": "06:30", "afternoonTime": "15:30", "active": True},
            {"id": "S2", "sequence": 2, "name": "Estate Gate", "morningTime": "06:40", "afternoonTime": "15:40", "active": True},
        ]
        self.rejected(self.push(c.ROUTE_PLAN, "BUS-01", plan(stops=stops), operation="update", who=self.admin), "already contains")

    def test_a_bad_time_format_is_refused(self):
        stops = [{"id": "S1", "sequence": 1, "name": "Estate Gate", "morningTime": "6:30am", "afternoonTime": "15:30", "active": True}]
        self.rejected(self.push(c.ROUTE_PLAN, "BUS-01", plan(stops=stops), operation="update", who=self.admin), "HH:mm")

    def test_deactivating_a_stop_with_a_rider_still_on_it_is_refused(self):
        self.ok(self.push(c.RIDER_ASSIGNMENT, "STU-1", rider(), who=self.admin))
        stops = [{"id": "S1", "sequence": 1, "name": "Estate Gate", "morningTime": "06:30", "afternoonTime": "15:30", "active": False}]
        self.rejected(self.push(c.ROUTE_PLAN, "BUS-01", plan(stops=stops), operation="update", who=self.admin), "still assigned")

    def test_deactivating_an_empty_stop_updates_the_route_stop_count(self):
        stops = [{"id": "S1", "sequence": 1, "name": "Estate Gate", "morningTime": "06:30", "afternoonTime": "15:30", "active": False}]
        self.ok(self.push(c.ROUTE_PLAN, "BUS-01", plan(stops=stops), operation="update", who=self.admin))
        self.assertEqual(self.stored(c.ROUTE, "BUS-01").payload["stops"], 0)


class VehicleClearanceTests(TransportTestCase):
    def test_holding_a_vehicle_needs_a_reason(self):
        self.rejected(self.push(c.VEHICLE_CLEARANCE, "BUS-01", clearance(status="held", note=""), who=self.admin), "reason")

    def test_holding_a_vehicle_also_marks_the_route_under_maintenance(self):
        self.ok(self.push(c.VEHICLE_CLEARANCE, "BUS-01", clearance(status="held", note="Brake check"), who=self.admin))
        self.assertEqual(self.stored(c.ROUTE, "BUS-01").payload["status"], "maintenance")

    def test_a_vehicle_with_an_open_blocking_defect_cannot_be_released(self):
        self.service_record(c.VEHICLE_DEFECT, "D1", {"routeId": "BUS-01", "vehicle": "Bus 1 - Toyota Hiace", "status": "open", "blocksTrip": True})
        self.rejected(self.push(c.VEHICLE_CLEARANCE, "BUS-01", clearance(status="released"), who=self.admin), "blocking safety")

    def test_a_driver_reads_only_their_own_routes_clearance(self):
        self.ok(self.push(c.VEHICLE_CLEARANCE, "BUS-02", clearance(route_id="BUS-02"), who=self.admin))
        self.ok(self.push(c.DRIVER_ASSIGNMENT, self.driver.id, assignment(str(self.driver.id), route_id="BUS-01"), who=self.admin))
        self.client.force_authenticate(self.driver.user)
        found = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id)}).json()["records"]
        seen = {r["entityId"] for r in found if r["entityType"] == c.VEHICLE_CLEARANCE}
        # BUS-01 gets an implicit clearance on first read of routes elsewhere in the app,
        # not created here - so this only asserts BUS-02's clearance never reaches the driver.
        self.assertNotIn("BUS-02", seen)
