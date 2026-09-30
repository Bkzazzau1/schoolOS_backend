from django.contrib.auth import get_user_model

from apps.schools.models import Membership, Role

from .. import constants as c
from .test_daily_run import DailyRunTestCase, assignment, route


class IncidentTestCase(DailyRunTestCase):
    def incident_id(self, epoch="1758790000000000"):
        return f"{self.driver.id}:incident:{self.today}:{epoch}"

    def incident(self, **over):
        body = {
            "id": self.incident_id(), "membershipId": str(self.driver.id), "routeId": "BUS-01",
            "vehicle": "wrong on purpose", "serviceDate": self.today, "category": "trafficDelay",
            "severity": "low", "phase": "morningRoute", "description": "Long queue at the market junction.",
            "reportedAt": "2026-09-25T06:45:00Z", "locationNote": "", "studentId": "", "studentName": "",
            "status": "queued", "requiresImmediateEscalation": False, "serverReference": "",
        }
        body.update(over)
        return body


class IncidentReportTests(IncidentTestCase):
    def test_the_driver_can_report_and_the_server_owns_what_matters(self):
        self.ok(self.push(c.INCIDENT, self.incident_id(), self.incident(), who=self.driver))
        stored = self.stored(c.INCIDENT, self.incident_id()).payload
        self.assertEqual(stored["status"], "submitted")
        self.assertEqual(stored["vehicle"], "Bus 1 - Toyota Hiace")  # from the real route, not the device
        self.assertEqual(stored["reportedByMembershipId"], str(self.driver.id))

    def test_urgency_is_the_servers_rule_not_a_flag_the_device_sends(self):
        cases = [("low", "trafficDelay", False), ("high", "trafficDelay", True),
                 ("critical", "other", True), ("low", "accident", True)]
        for i, (severity, category, urgent) in enumerate(cases):
            entity_id = self.incident_id(f"17587900000000{i:02d}")
            payload = self.incident(id=entity_id, severity=severity, category=category, requiresImmediateEscalation=not urgent)
            self.ok(self.push(c.INCIDENT, entity_id, payload, who=self.driver))
            self.assertIs(self.stored(c.INCIDENT, entity_id).payload["requiresImmediateEscalation"], urgent, (severity, category))

    def test_only_a_driver_reports(self):
        self.rejected(self.push(c.INCIDENT, self.incident_id(), self.incident(), who=self.admin), "Only the Driver")

    def test_a_short_description_is_refused(self):
        self.rejected(self.push(c.INCIDENT, self.incident_id(), self.incident(description="Too short"), who=self.driver), "10 characters")

    def test_bad_categories_severities_and_phases_are_refused(self):
        for over in ({"category": "nonsense"}, {"severity": "catastrophic"}, {"phase": "midnight"}):
            self.rejected(self.push(c.INCIDENT, self.incident_id(), self.incident(**over), who=self.driver))

    def test_it_must_be_todays_and_on_the_drivers_real_route(self):
        self.rejected(self.push(c.INCIDENT, self.incident_id(), self.incident(routeId="BUS-99"), who=self.driver), "routeId")
        stale = f"{self.driver.id}:incident:2020-01-01:1758790000000000"
        self.rejected(self.push(c.INCIDENT, stale, self.incident(id=stale, serviceDate="2020-01-01"), who=self.driver), "today")
        theirs = f"someone-else:incident:{self.today}:1758790000000000"
        self.rejected(self.push(c.INCIDENT, theirs, self.incident(id=theirs), who=self.driver))

    def test_a_student_named_must_really_be_on_the_manifest_and_the_name_is_the_servers(self):
        self.rejected(self.push(c.INCIDENT, self.incident_id(), self.incident(studentId="STU-NOPE"), who=self.driver), "manifest")
        self.ok(self.push(c.INCIDENT, self.incident_id(), self.incident(studentId="STU-1", studentName="Someone Else"), who=self.driver))
        self.assertEqual(self.stored(c.INCIDENT, self.incident_id()).payload["studentName"], "A Student")


class IncidentReviewTests(IncidentTestCase):
    def setUp(self):
        super().setUp()
        self.ok(self.push(c.INCIDENT, self.incident_id(), self.incident(), who=self.driver))

    def review(self, status, note="", who=None):
        return self.push(c.INCIDENT, self.incident_id(), {"status": status, "managementNote": note}, operation="update", who=who or self.admin)

    def test_transport_control_moves_it_forward(self):
        self.ok(self.review("acknowledged"))
        self.ok(self.review("underReview", "Calling the driver."))
        stored = self.stored(c.INCIDENT, self.incident_id()).payload
        self.assertEqual((stored["status"], stored["updatedByMembershipId"]), ("underReview", str(self.admin.id)))

    def test_resolving_needs_a_note_and_a_resolved_incident_stays_resolved(self):
        self.rejected(self.review("resolved", ""), "resolution note")
        self.ok(self.review("resolved", "Driver rerouted."))
        self.assertFalse(self.stored(c.INCIDENT, self.incident_id()).payload["requiresTransportReview"])
        self.rejected(self.review("underReview", "Reopen?"), "already resolved")

    def test_review_cannot_rewrite_what_was_reported(self):
        payload = {"status": "acknowledged", "description": "Rewritten by a manager.", "severity": "low", "routeId": "BUS-99"}
        self.ok(self.push(c.INCIDENT, self.incident_id(), payload, operation="update", who=self.admin))
        stored = self.stored(c.INCIDENT, self.incident_id()).payload
        self.assertEqual((stored["description"], stored["routeId"]), ("Long queue at the market junction.", "BUS-01"))

    def test_a_driver_and_an_unrelated_role_cannot_review(self):
        self.rejected(self.review("acknowledged", who=self.driver), "Only Transport Control")
        self.rejected(self.review("acknowledged", who=self.members["teacher"]), "Only Transport Control")

    def test_an_incident_is_never_deleted(self):
        self.rejected(self.push(c.INCIDENT, self.incident_id(), operation="delete", who=self.admin), "never deleted")

    def test_who_reads_what(self):
        def seen(who):
            self.client.force_authenticate(who.user)
            found = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id)}).json()["records"]
            return {r["entityId"] for r in found if r["entityType"] == c.INCIDENT}

        for who in (self.owner, self.admin, self.members["principal"], self.driver):
            self.assertIn(self.incident_id(), seen(who))
        for role in ("teacher", "parent", "staff", "accountant"):
            self.assertNotIn(self.incident_id(), seen(self.members[role]), role)


class CaseEventTests(IncidentTestCase):
    def payload(self, **over):
        body = {"id": "ce1", "caseId": "case-1", "caseType": "incident", "routeId": "BUS-01", "vehicle": "Bus 1",
                "eventType": "transport_incident_acknowledged", "status": "acknowledged", "note": "Seen."}
        body.update(over)
        return body

    def test_management_records_it_and_it_is_never_changed(self):
        self.ok(self.push(c.CASE_EVENT, "ce1", self.payload(), who=self.admin))
        self.assertEqual(self.stored(c.CASE_EVENT, "ce1").payload["note"], "Seen.")
        self.rejected(self.push(c.CASE_EVENT, "ce1", self.payload(), operation="update", who=self.admin))

    def test_a_driver_cannot_write_one_and_the_case_type_must_be_real(self):
        self.rejected(self.push(c.CASE_EVENT, "ce1", self.payload(), who=self.driver), "role may not")
        self.rejected(self.push(c.CASE_EVENT, "ce2", self.payload(id="ce2", caseType="gossip"), who=self.admin), "caseType")


class DriverMessageTests(IncidentTestCase):
    """The one real channel between a Driver and Transport Control (apps/transport/driver_messages.py).
    A second Driver, with their own real route and their own real, separate thread, proves messages
    never cross between Drivers."""

    def setUp(self):
        super().setUp()
        other_user = get_user_model().objects.create_user("other-driver@school.ng", "a-long-test-password-1")
        self.other_driver = Membership.objects.create(user=other_user, school=self.school, role=Role.DRIVER)
        self.ok(self.push(c.ROUTE, "BUS-02", route(id="BUS-02", vehicle="Bus 2 - Ford Transit"), who=self.admin))
        self.ok(self.push(c.DRIVER_ASSIGNMENT, self.other_driver.id, assignment(str(self.other_driver.id), route_id="BUS-02"), who=self.admin))

    def message_id(self, who=None, epoch="1758790000000000"):
        return f"LOCAL-{(who or self.driver).id}-{epoch}"

    def thread_id(self, driver=None):
        return f"driver-thread-{(driver or self.driver).id}"

    def message(self, who=None, target_driver=None, **over):
        who = who or self.driver
        target_driver = target_driver or (who if who.role == "driver" else self.driver)
        body = {
            "messageId": self.message_id(who), "threadId": self.thread_id(target_driver),
            "body": "Running ten minutes late.", "createdAt": "2026-09-25T06:45:00Z",
        }
        if who.role != "driver":
            body["driverMembershipId"] = str(target_driver.id)
        body.update(over)
        return body

    def send(self, who=None, target_driver=None, **over):
        who = who or self.driver
        payload = self.message(who=who, target_driver=target_driver, **over)
        return self.push(c.DRIVER_MESSAGE, payload["messageId"], payload, who=who)

    def test_a_driver_can_send_and_the_server_stamps_the_sender_route_and_vehicle(self):
        self.ok(self.send())
        stored = self.stored(c.DRIVER_MESSAGE, self.message_id()).payload
        self.assertEqual(
            (stored["senderMembershipId"], stored["senderRole"], stored["driverMembershipId"], stored["vehicle"]),
            (str(self.driver.id), "driver", str(self.driver.id), "Bus 1 - Toyota Hiace"),
        )

    def test_transport_control_can_reply_into_a_real_drivers_thread(self):
        self.ok(self.send(who=self.admin))
        stored = self.stored(c.DRIVER_MESSAGE, self.message_id(self.admin)).payload
        self.assertEqual(
            (stored["senderMembershipId"], stored["senderRole"], stored["driverMembershipId"], stored["routeId"]),
            (str(self.admin.id), "administrator", str(self.driver.id), "BUS-01"),
        )

    def test_a_drivers_own_route_is_always_the_servers_not_the_devices(self):
        self.ok(self.send(routeId="BUS-99"))
        self.assertEqual(self.stored(c.DRIVER_MESSAGE, self.message_id()).payload["routeId"], "BUS-01")

    def test_transport_control_must_name_a_real_currently_assigned_driver(self):
        self.rejected(self.send(who=self.admin, driverMembershipId="not-a-real-driver"), "must be a real")
        self.rejected(self.send(who=self.admin, driverMembershipId=str(self.owner.id)), "must be a real")

    def test_a_thread_id_that_does_not_match_the_real_driver_is_refused(self):
        self.rejected(self.send(threadId="driver-thread-someone-else"), "own channel")
        self.rejected(self.send(who=self.admin, threadId=self.thread_id(self.other_driver)), "own channel")

    def test_messages_never_cross_between_drivers(self):
        self.ok(self.send())
        self.ok(self.send(who=self.other_driver))
        self.ok(self.send(who=self.admin))

        def seen(who):
            self.client.force_authenticate(who.user)
            found = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id)}).json()["records"]
            return {r["entityId"] for r in found if r["entityType"] == c.DRIVER_MESSAGE}

        mine, admins_reply = self.message_id(), self.message_id(self.admin)
        others = self.message_id(self.other_driver)
        self.assertEqual(seen(self.driver), {mine, admins_reply})
        self.assertEqual(seen(self.other_driver), {others})
        self.assertEqual(seen(self.admin), {mine, admins_reply, others})
        self.assertIn(mine, seen(self.members["principal"]))

    def test_an_unrelated_role_cannot_send_transport_messages(self):
        self.rejected(self.send(who=self.members["teacher"]), "role may not")

    def test_an_empty_or_huge_body_is_refused(self):
        self.rejected(self.send(body="  "), "body")
        self.rejected(self.send(body="x" * 2001), "too long")

    def test_it_is_never_changed_or_removed_once_sent(self):
        self.ok(self.send())
        self.rejected(self.push(c.DRIVER_MESSAGE, self.message_id(), self.message(), operation="update", who=self.driver), "never changed")
        self.rejected(self.push(c.DRIVER_MESSAGE, self.message_id(), operation="delete", who=self.driver), "never changed")


class DriverReceiptTests(IncidentTestCase):
    def test_a_thread_seen_receipt(self):
        rid = f"{self.driver.id}:thread-seen:driver-thread-transport-control:1758790000000000"
        body = {"id": rid, "threadId": "driver-thread-transport-control", "routeId": "BUS-01", "seenAt": "2026-09-25T06:46:00Z", "clientState": "queued"}
        self.ok(self.push(c.DRIVER_MESSAGE_RECEIPT, rid, body, who=self.driver))
        stored = self.stored(c.DRIVER_MESSAGE_RECEIPT, rid).payload
        self.assertEqual((stored["threadId"], stored["membershipId"]), ("driver-thread-transport-control", str(self.driver.id)))
        self.rejected(self.push(c.DRIVER_MESSAGE_RECEIPT, rid, body, operation="update", who=self.driver), "never changed")

    def test_an_alert_read_receipt_and_a_forged_subject_is_refused(self):
        rid = f"{self.driver.id}:alert-read:driver-alert-001:1758790000000000"
        body = {"id": rid, "alertId": "driver-alert-001", "routeId": "BUS-01", "readAt": "2026-09-25T06:47:00Z"}
        self.ok(self.push(c.DRIVER_ALERT_RECEIPT, rid, body, who=self.driver))
        forged = f"{self.driver.id}:alert-read:driver-alert-002:1758790000000001"
        self.rejected(self.push(c.DRIVER_ALERT_RECEIPT, forged, {**body, "id": forged}, who=self.driver), "alertId")

    def test_someone_elses_receipt_is_refused(self):
        rid = "someone-else:thread-seen:t1:1758790000000000"
        body = {"id": rid, "threadId": "t1", "routeId": "BUS-01", "seenAt": ""}
        self.rejected(self.push(c.DRIVER_MESSAGE_RECEIPT, rid, body, who=self.driver), "does not belong")
