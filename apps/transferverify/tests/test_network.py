from apps.core.errors import Rejected
from apps.staff.tests.helpers import StaffTestCase
from apps.students.models import GuardianLink, Student

from .. import network, services
from ..models import (
    AssociationStatus,
    NetworkSearchAudit,
    SchoolAssociationMembership,
    SchoolProprietorAssociation,
    StudentNetworkEnrollment,
    TransferAlert,
    TransferAlertState,
)


def make_association(name="Kaduna Private Schools Association"):
    return SchoolProprietorAssociation.objects.create(name=name, status=AssociationStatus.ACTIVE)


def bad_debt(student, classifier):
    return services.classify(
        membership=classifier,
        payload={
            "id": f"BDC-{student.id}", "studentId": str(student.id),
            "outstandingAmountMinor": 15000000, "reason": "Withdrew without financial clearance",
        },
    )


class NetworkTestCase(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.student = Student.objects.create(
            school=self.school, admission_number="ADM-7001", student_code="STU-7001",
            first_name="Chidera", surname="Okafor",
        )
        GuardianLink.objects.create(student=self.student, name="Mrs Okafor", phone="08031234567", is_primary=True)


class EnrollmentTests(NetworkTestCase):
    def test_the_first_touch_creates_a_new_identity_with_the_normalized_phone(self):
        enrollment = network.enrollment_for(school=self.school, student=self.student, membership=self.owner)
        self.assertEqual(enrollment.guardian_phone_e164, "+2348031234567")
        self.assertEqual(enrollment.guardian_phone_history, [])
        self.assertEqual(StudentNetworkEnrollment.objects.filter(school=self.school, student=self.student).count(), 1)

    def test_the_second_touch_reuses_the_same_identity(self):
        first = network.enrollment_for(school=self.school, student=self.student, membership=self.owner)
        second = network.enrollment_for(school=self.school, student=self.student, membership=self.owner)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.network_identity_id, second.network_identity_id)

    def test_a_changed_phone_updates_current_and_keeps_history(self):
        network.enrollment_for(school=self.school, student=self.student, membership=self.owner)
        GuardianLink.objects.filter(student=self.student).update(phone="08099998888")
        updated = network.enrollment_for(school=self.school, student=self.student, membership=self.owner)
        self.assertEqual(updated.guardian_phone_e164, "+2348099998888")
        self.assertEqual(len(updated.guardian_phone_history), 1)
        self.assertEqual(updated.guardian_phone_history[0]["phone"], "+2348031234567")


class AlertLifecycleTests(NetworkTestCase):
    def test_publishing_creates_a_real_transfer_alert_snapshot(self):
        item = bad_debt(self.student, self.owner)
        services.advance_status(membership=self.owner, external_id=item.external_id, status="bad_debt")
        published = services.publish_to_transferverify(
            membership=self.owner, external_id=item.external_id, reason="withdrew_without_clearance"
        )
        alert = TransferAlert.objects.get(source_classification=published)
        self.assertEqual(alert.state, TransferAlertState.ACTIVE)
        self.assertEqual(alert.snapshot_outstanding_amount_minor, 15000000)
        self.assertEqual(alert.source_school_id, self.school.id)
        enrollment = StudentNetworkEnrollment.objects.get(school=self.school, student=self.student)
        self.assertEqual(alert.network_identity_id, enrollment.network_identity_id)

    def test_withdrawing_publication_withdraws_the_alert_without_deleting_it(self):
        item = bad_debt(self.student, self.owner)
        services.advance_status(membership=self.owner, external_id=item.external_id, status="bad_debt")
        published = services.publish_to_transferverify(
            membership=self.owner, external_id=item.external_id, reason="withdrew_without_clearance"
        )
        services.withdraw_publication(membership=self.owner, external_id=published.external_id)
        alert = TransferAlert.objects.get(source_classification_id=published.id)
        self.assertEqual(alert.state, TransferAlertState.WITHDRAWN)
        self.assertIsNotNone(alert.withdrawn_at)

    def test_republishing_after_withdrawal_reopens_the_same_alert(self):
        item = bad_debt(self.student, self.owner)
        services.advance_status(membership=self.owner, external_id=item.external_id, status="bad_debt")
        first = services.publish_to_transferverify(
            membership=self.owner, external_id=item.external_id, reason="withdrew_without_clearance"
        )
        first_alert_id = TransferAlert.objects.get(source_classification=first).id
        services.withdraw_publication(membership=self.owner, external_id=first.external_id)
        second = services.publish_to_transferverify(
            membership=self.owner, external_id=first.external_id, reason="guardian_unreachable"
        )
        alert = TransferAlert.objects.get(source_classification=second)
        self.assertEqual(alert.id, first_alert_id)
        self.assertEqual(alert.state, TransferAlertState.ACTIVE)

    def test_resolving_a_published_case_resolves_its_alert_too(self):
        item = bad_debt(self.student, self.owner)
        services.advance_status(membership=self.owner, external_id=item.external_id, status="bad_debt")
        published = services.publish_to_transferverify(
            membership=self.owner, external_id=item.external_id, reason="withdrew_without_clearance"
        )
        services.resolve(membership=self.owner, external_id=published.external_id, note="Paid in full.")
        alert = TransferAlert.objects.get(source_classification_id=published.id)
        self.assertEqual(alert.state, TransferAlertState.RESOLVED)


class PhoneMatchTests(NetworkTestCase):
    def setUp(self):
        super().setUp()
        self.association = make_association()
        # Both schools share this active association.
        SchoolAssociationMembership.objects.create(
            association=self.association, school=self.school, requested_by=self.owner, status="active",
        )
        SchoolAssociationMembership.objects.create(
            association=self.association, school=self.other_school, requested_by=self.other_owner, status="active",
        )
        item = bad_debt(self.student, self.owner)
        services.advance_status(membership=self.owner, external_id=item.external_id, status="bad_debt")
        self.published = services.publish_to_transferverify(
            membership=self.owner, external_id=item.external_id, reason="withdrew_without_clearance",
            association_ids=[str(self.association.id)],
        )

    def test_a_shared_association_surfaces_the_candidate_by_current_phone(self):
        results = network.match_by_phone(membership=self.other_owner, phone="08031234567")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["confidence"], "candidate_match")
        self.assertEqual(results[0]["sourceSchoolName"], self.school.name)
        self.assertNotIn("outstandingAmountMinor", results[0])  # no raw amount at candidate confidence

    def test_a_historical_phone_number_still_matches(self):
        GuardianLink.objects.filter(student=self.student).update(phone="08099998888")
        network.enrollment_for(school=self.school, student=self.student, membership=self.owner)
        results = network.match_by_phone(membership=self.other_owner, phone="08031234567")
        self.assertEqual(len(results), 1)

    def test_no_shared_association_means_no_match_even_with_the_same_phone(self):
        SchoolAssociationMembership.objects.filter(school=self.other_school).update(status="exited")
        results = network.match_by_phone(membership=self.other_owner, phone="08031234567")
        self.assertEqual(results, [])

    def test_an_alert_scoped_to_a_different_association_is_not_discoverable(self):
        other_association = make_association(name="A different association")
        SchoolAssociationMembership.objects.create(
            association=other_association, school=self.other_school, requested_by=self.other_owner, status="active",
        )
        # self.school never joined other_association, so its alert (scoped only to self.association)
        # must not surface just because other_school now also belongs to a second, unrelated association.
        results = network.match_by_phone(membership=self.other_owner, phone="08031234567")
        self.assertEqual(len(results), 1)  # still only the one legitimately shared match

    def test_an_unrelated_phone_number_finds_nothing(self):
        results = network.match_by_phone(membership=self.other_owner, phone="08000000000")
        self.assertEqual(results, [])

    def test_a_school_never_matches_its_own_alert_against_itself(self):
        results = network.match_by_phone(membership=self.owner, phone="08031234567")
        self.assertEqual(results, [])

    def test_an_invalid_phone_is_refused(self):
        with self.assertRaises(Rejected):
            network.match_by_phone(membership=self.other_owner, phone="not a phone")

    def test_every_search_is_audited_without_storing_the_raw_phone(self):
        network.match_by_phone(membership=self.other_owner, phone="08031234567")
        audit = NetworkSearchAudit.objects.get(searching_school=self.other_school)
        self.assertEqual(audit.result_count, 1)
        self.assertEqual(audit.searching_membership_id, self.other_owner.id)
        self.assertNotIn("phone", [f.name for f in audit._meta.get_fields()])


class NetworkMatchEndpointTests(NetworkTestCase):
    def setUp(self):
        super().setUp()
        self.association = make_association()
        SchoolAssociationMembership.objects.create(
            association=self.association, school=self.school, requested_by=self.owner, status="active",
        )
        SchoolAssociationMembership.objects.create(
            association=self.association, school=self.other_school, requested_by=self.other_owner, status="active",
        )
        item = bad_debt(self.student, self.owner)
        services.advance_status(membership=self.owner, external_id=item.external_id, status="bad_debt")
        services.publish_to_transferverify(
            membership=self.owner, external_id=item.external_id, reason="withdrew_without_clearance",
            association_ids=[str(self.association.id)],
        )

    def test_the_endpoint_returns_the_matching_candidate_over_http(self):
        self.client.force_authenticate(self.other_owner.user)
        response = self.client.post(
            f"/api/v1/schools/{self.other_school.id}/transferverify/network/match/",
            {"phone": "08031234567"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        candidates = response.json()["candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["sourceSchoolName"], self.school.name)

    def test_a_role_without_admissions_access_cannot_search(self):
        self.client.force_authenticate(self.other_principal.user)
        response = self.client.post(
            f"/api/v1/schools/{self.other_school.id}/transferverify/network/match/",
            {"phone": "08031234567"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
