from apps.staff.tests.helpers import StaffTestCase
from apps.students.models import Student
from apps.sync.models import SyncRecord
from apps.transferverify.models import BadDebtClassification, BadDebtEvent, BadDebtStatus

TYPE = "transferverify_bad_debt_classification"


def classify_body(id="BDC-2026-001", **over):
    body = {
        "id": id, "action": "classify", "studentId": "",
        "outstandingAmountMinor": 24500000, "reason": "Withdrew without financial clearance",
        "notes": "Two recovery calls made, no response.", "evidenceReference": "Ledger folder 4",
    }
    body.update(over)
    return body


class BadDebtClassificationTestCase(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.finance = self.members["accountant"]
        self.student = Student.objects.create(
            school=self.school, admission_number="ADM-9001", student_code="STU-9001",
            first_name="Ibrahim", surname="Musa",
        )
        self.other_student = Student.objects.create(
            school=self.school, admission_number="ADM-9002", student_code="STU-9002",
            first_name="Amina", surname="Bello",
        )

    def classify(self, who=None, id="BDC-2026-001", **over):
        body = classify_body(id, **over)
        if "studentId" not in over:
            body["studentId"] = str(self.student.id)
        return self.push(TYPE, id, body, who=who or self.owner)

    def act(self, action, who=None, id="BDC-2026-001", **over):
        stored = self.stored(TYPE, id).payload
        return self.push(TYPE, id, {**stored, "action": action, **over}, operation="update", who=who or self.owner)

    def stored_classification(self, id="BDC-2026-001"):
        return self.stored(TYPE, id).payload

    def give_duty(self, member, duty="finance.bad_debt_classification", status="active"):
        SyncRecord.objects.update_or_create(
            school=self.school, entity_type="owner_job_assignment", entity_id=f"JOB-{member.id}",
            defaults={"payload": {"registeredStaffId": f"S-{member.id}", "duties": [duty], "status": status,
                                   "membershipId": str(member.id)}},
        )


class ClassifyingTests(BadDebtClassificationTestCase):
    def test_the_owner_classifies_and_the_server_records_who_and_when(self):
        self.ok(self.classify())
        p = self.stored_classification()
        self.assertEqual(p["status"], "outstanding")
        self.assertEqual(p["studentId"], str(self.student.id))
        self.assertEqual(p["studentName"], "Ibrahim Musa")
        self.assertEqual(p["classifiedByMembershipId"], str(self.owner.id))
        self.assertEqual(p["outstandingAmountMinor"], 24500000)
        self.assertFalse(p["publishedToTransferVerify"])
        self.assertIsNone(p["currentCanonicalBalanceMinor"])

    def test_others_cannot_classify_unless_the_owner_gave_them_the_duty(self):
        for role in ("principal", "administrator", "teacher", "staff", "parent", "student"):
            self.rejected(self.classify(who=self.members[role]), "specifically authorized")
        self.rejected(self.classify(who=self.finance), "specifically authorized")
        self.give_duty(self.finance, duty="finance.reports")  # a different duty
        self.rejected(self.classify(who=self.finance), "specifically authorized")
        self.give_duty(self.finance)
        self.ok(self.classify(who=self.finance))

    def test_the_amount_must_make_sense(self):
        for over in ({"outstandingAmountMinor": 0}, {"outstandingAmountMinor": -5}, {"outstandingAmountMinor": "5"}):
            self.rejected(self.classify(**over))
        self.assertFalse(BadDebtClassification.objects.exists())

    def test_a_student_from_another_school_is_refused(self):
        other_student = Student.objects.create(
            school=self.other_school, admission_number="ADM-1", student_code="STU-OTH-1",
            first_name="Foreign", surname="Student",
        )
        self.rejected(self.classify(studentId=str(other_student.id)), "does not exist")

    def test_a_second_open_classification_for_the_same_student_is_refused(self):
        self.ok(self.classify())
        self.rejected(self.classify(id="BDC-2026-002"), "already has an open bad debt classification")
        # A different student is fine.
        self.ok(self.classify(id="BDC-2026-003", studentId=str(self.other_student.id)))

    def test_it_cannot_be_deleted_or_used_across_schools(self):
        self.ok(self.classify())
        self.rejected(self.push(TYPE, "BDC-2026-001", operation="delete", who=self.owner), "cannot be deleted")
        self.assertEqual(
            self.push(TYPE, "BDC-2026-099", classify_body("BDC-2026-099", studentId=str(self.student.id)),
                      who=self.other_owner, school=self.school).status_code,
            403,
        )

    def test_a_classification_event_is_recorded(self):
        self.ok(self.classify())
        item = BadDebtClassification.objects.get(school=self.school, external_id="BDC-2026-001")
        events = list(BadDebtEvent.objects.filter(classification=item).order_by("revision"))
        self.assertEqual([e.action for e in events], ["classified"])
        self.assertEqual(events[0].actor_membership_id, self.owner.id)


class ProgressionTests(BadDebtClassificationTestCase):
    def setUp(self):
        super().setUp()
        self.ok(self.classify())

    def test_status_moves_forward_through_recovery_to_bad_debt(self):
        self.ok(self.act("advanceStatus", status="recovery_in_progress"))
        self.assertEqual(self.stored_classification()["status"], "recovery_in_progress")
        self.ok(self.act("advanceStatus", status="bad_debt"))
        self.assertEqual(self.stored_classification()["status"], "bad_debt")

    def test_status_cannot_move_backward_or_sideways(self):
        self.ok(self.act("advanceStatus", status="bad_debt"))
        self.rejected(self.act("advanceStatus", status="recovery_in_progress"), "only move forward")

    def test_advance_status_cannot_target_outstanding_or_resolved_directly(self):
        self.rejected(self.act("advanceStatus", status="outstanding"))
        self.rejected(self.act("advanceStatus", status="resolved"))

    def test_editing_updates_the_snapshot_without_changing_status(self):
        self.ok(self.act("update", outstandingAmountMinor=30000000, reason="Balance recalculated", notes="Updated after review."))
        p = self.stored_classification()
        self.assertEqual((p["status"], p["outstandingAmountMinor"], p["reason"]), ("outstanding", 30000000, "Balance recalculated"))

    def test_resolving_closes_it_and_is_never_deleted(self):
        self.ok(self.act("resolve", note="Guardian paid the balance in full."))
        p = self.stored_classification()
        self.assertEqual(p["status"], "resolved")
        self.assertEqual(p["resolutionNote"], "Guardian paid the balance in full.")
        self.assertTrue(p["resolvedAt"])
        self.assertTrue(BadDebtClassification.objects.filter(external_id="BDC-2026-001").exists())

    def test_resolving_twice_is_harmless_and_keeps_the_first_resolution(self):
        self.ok(self.act("resolve", note="First reason."))
        self.ok(self.act("resolve", note="Different reason."))
        self.assertEqual(self.stored_classification()["resolutionNote"], "First reason.")

    def test_a_resolved_classification_cannot_be_edited_or_advanced(self):
        self.ok(self.act("resolve"))
        self.rejected(self.act("update", reason="Trying to change it"), "resolved")
        self.rejected(self.act("advanceStatus", status="bad_debt"), "already resolved")

    def test_events_accumulate_in_order(self):
        self.ok(self.act("advanceStatus", status="recovery_in_progress"))
        self.ok(self.act("update", notes="Called again."))
        self.ok(self.act("resolve", note="Paid."))
        item = BadDebtClassification.objects.get(school=self.school, external_id="BDC-2026-001")
        actions = list(BadDebtEvent.objects.filter(classification=item).order_by("revision").values_list("action", flat=True))
        self.assertEqual(actions, ["classified", "status_advanced", "updated", "resolved"])


class PublishingTests(BadDebtClassificationTestCase):
    def setUp(self):
        super().setUp()
        self.ok(self.classify())

    def publish(self, who=None, reason="withdrew_without_clearance", **over):
        return self.act("publish", who=who, reason=reason, **over)

    def withdraw(self, who=None):
        return self.act("withdrawPublication", who=who)

    def test_only_a_bad_debt_status_case_can_be_published(self):
        self.rejected(self.publish(), "classified as bad debt")
        self.ok(self.act("advanceStatus", status="recovery_in_progress"))
        self.rejected(self.publish(), "classified as bad debt")
        self.ok(self.act("advanceStatus", status="bad_debt"))
        self.ok(self.publish())

    def test_publishing_records_who_when_and_why(self):
        self.ok(self.act("advanceStatus", status="bad_debt"))
        self.ok(self.publish(reason="guardian_unreachable", note="No answer after three calls."))
        p = self.stored_classification()
        self.assertTrue(p["publishedToTransferVerify"])
        self.assertEqual(p["publishedByMembershipId"], str(self.owner.id))
        self.assertEqual(p["publicationReason"], "guardian_unreachable")
        self.assertEqual(p["publicationNote"], "No answer after three calls.")
        self.assertTrue(p["publishedAt"])
        self.assertEqual(p["associationScope"], [])  # nothing to scope to yet

    def test_a_reason_is_required_and_must_be_a_real_choice(self):
        self.ok(self.act("advanceStatus", status="bad_debt"))
        self.rejected(self.publish(reason="fraud"), "not a valid choice")

    def test_publishing_is_owner_only_even_for_a_classify_duty_holder(self):
        self.give_duty(self.finance)
        self.ok(self.act("advanceStatus", status="bad_debt"))
        self.rejected(self.publish(who=self.finance), "Only the owner can publish")
        self.ok(self.publish())

    def test_it_cannot_be_published_twice(self):
        self.ok(self.act("advanceStatus", status="bad_debt"))
        self.ok(self.publish())
        self.rejected(self.publish(), "already been published")

    def test_a_published_case_cannot_be_edited_until_withdrawn(self):
        self.ok(self.act("advanceStatus", status="bad_debt"))
        self.ok(self.publish())
        self.rejected(self.act("update", notes="Trying to change it"), "Withdraw the publication")
        self.ok(self.withdraw())
        self.ok(self.act("update", notes="Now editable again."))
        self.assertEqual(self.stored_classification()["notes"], "Now editable again.")

    def test_withdrawing_clears_publication_but_keeps_the_classification_and_its_history(self):
        self.ok(self.act("advanceStatus", status="bad_debt"))
        self.ok(self.publish(reason="arrangement_defaulted"))
        self.ok(self.withdraw())
        p = self.stored_classification()
        self.assertEqual(
            (p["publishedToTransferVerify"], p["publishedByMembershipId"], p["publishedAt"], p["publicationReason"]),
            (False, None, None, ""),
        )
        self.assertEqual(p["status"], "bad_debt")  # the classification itself is untouched

    def test_withdrawing_when_not_published_is_refused(self):
        self.rejected(self.withdraw(), "not been published")

    def test_withdrawing_is_also_owner_only(self):
        self.give_duty(self.finance)
        self.ok(self.act("advanceStatus", status="bad_debt"))
        self.ok(self.publish())
        self.rejected(self.withdraw(who=self.finance), "Only the owner can publish")

    def test_publish_and_withdraw_are_both_audited(self):
        self.ok(self.act("advanceStatus", status="bad_debt"))
        self.ok(self.publish())
        self.ok(self.withdraw())
        item = BadDebtClassification.objects.get(school=self.school, external_id="BDC-2026-001")
        actions = list(BadDebtEvent.objects.filter(classification=item).order_by("revision").values_list("action", flat=True))
        self.assertEqual(actions, ["classified", "status_advanced", "published", "publication_withdrawn"])

    def test_resolving_a_published_case_keeps_the_publication_intact(self):
        self.ok(self.act("advanceStatus", status="bad_debt"))
        self.ok(self.publish())
        self.ok(self.act("resolve", note="Guardian paid in full."))
        p = self.stored_classification()
        self.assertEqual(p["status"], "resolved")
        self.assertTrue(p["publishedToTransferVerify"])  # a resolved case stays a real, auditable published record


class WhoReceivesItTests(BadDebtClassificationTestCase):
    def kinds(self, who):
        self.client.force_authenticate(who.user)
        found = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id)}).json()["records"]
        return {r["entityType"] for r in found}

    def test_only_the_owner_and_someone_with_the_duty_receive_it(self):
        self.ok(self.classify())
        self.assertIn(TYPE, self.kinds(self.owner))
        for role in ("principal", "administrator", "teacher", "staff", "parent", "student"):
            self.assertNotIn(TYPE, self.kinds(self.members[role]), role)
        self.assertNotIn(TYPE, self.kinds(self.finance))
        self.give_duty(self.finance)
        self.assertIn(TYPE, self.kinds(self.finance))
