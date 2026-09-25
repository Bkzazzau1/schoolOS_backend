from apps.core.errors import Rejected

from .. import services, verification
from ..models import (
    SchoolAssociationMembership,
    TransferAlert,
    TransferAlertState,
    TransferVerificationRequestStatus,
)
from .test_network import NetworkTestCase, bad_debt, make_association


class VerificationTestCase(NetworkTestCase):
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
        self.published = services.publish_to_transferverify(
            membership=self.owner, external_id=item.external_id, reason="withdrew_without_clearance",
            association_ids=[str(self.association.id)],
        )
        self.alert = TransferAlert.objects.get(source_classification=self.published)


class SendingTests(VerificationTestCase):
    def test_a_shared_association_can_ask_and_it_pends_the_alert(self):
        item = verification.send_request(membership=self.other_owner, transfer_alert_id=str(self.alert.id), note="Considering admission.")
        self.assertEqual(item.status, TransferVerificationRequestStatus.PENDING)
        self.assertEqual(item.requesting_school_id, self.other_school.id)
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.state, TransferAlertState.VERIFICATION_PENDING)

    def test_a_school_without_a_shared_association_cannot_ask(self):
        SchoolAssociationMembership.objects.filter(school=self.other_school).update(status="exited")
        with self.assertRaises(Rejected):
            verification.send_request(membership=self.other_owner, transfer_alert_id=str(self.alert.id))

    def test_the_source_school_cannot_ask_about_its_own_case(self):
        with self.assertRaises(Rejected):
            verification.send_request(membership=self.owner, transfer_alert_id=str(self.alert.id))

    def test_a_second_pending_request_for_the_same_case_is_refused(self):
        verification.send_request(membership=self.other_owner, transfer_alert_id=str(self.alert.id))
        with self.assertRaises(Rejected):
            verification.send_request(membership=self.other_owner, transfer_alert_id=str(self.alert.id))

    def test_a_role_outside_admissions_cannot_ask(self):
        with self.assertRaises(Rejected):
            verification.send_request(membership=self.other_principal, transfer_alert_id=str(self.alert.id))


class RespondingTests(VerificationTestCase):
    def setUp(self):
        super().setUp()
        self.item = verification.send_request(membership=self.other_owner, transfer_alert_id=str(self.alert.id))

    def test_the_source_owner_confirms_and_the_alert_returns_to_active(self):
        answered = verification.respond_to_request(
            membership=self.owner, request_id=str(self.item.id), decision="confirmed", note="Correct, unresolved."
        )
        self.assertEqual(answered.status, TransferVerificationRequestStatus.CONFIRMED)
        self.assertEqual(answered.response_status_snapshot, "bad_debt")
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.state, TransferAlertState.ACTIVE)

    def test_the_source_owner_rejects_with_a_note(self):
        answered = verification.respond_to_request(
            membership=self.owner, request_id=str(self.item.id), decision="rejected", note="Different family."
        )
        self.assertEqual(answered.status, TransferVerificationRequestStatus.REJECTED)
        self.assertEqual(answered.response_note, "Different family.")

    def test_the_requesting_school_cannot_respond_to_its_own_request(self):
        with self.assertRaises(Rejected):
            verification.respond_to_request(membership=self.other_owner, request_id=str(self.item.id), decision="confirmed")

    def test_a_finance_delegate_at_the_source_school_cannot_respond(self):
        with self.assertRaises(Rejected):
            verification.respond_to_request(membership=self.members["accountant"], request_id=str(self.item.id), decision="confirmed")

    def test_answering_twice_is_refused(self):
        verification.respond_to_request(membership=self.owner, request_id=str(self.item.id), decision="confirmed")
        with self.assertRaises(Rejected):
            verification.respond_to_request(membership=self.owner, request_id=str(self.item.id), decision="rejected")

    def test_an_invalid_decision_is_refused(self):
        with self.assertRaises(Rejected):
            verification.respond_to_request(membership=self.owner, request_id=str(self.item.id), decision="maybe")


class CancellingTests(VerificationTestCase):
    def setUp(self):
        super().setUp()
        self.item = verification.send_request(membership=self.other_owner, transfer_alert_id=str(self.alert.id))

    def test_the_requesting_school_cancels_and_the_alert_returns_to_active(self):
        cancelled = verification.cancel_request(membership=self.other_owner, request_id=str(self.item.id))
        self.assertEqual(cancelled.status, TransferVerificationRequestStatus.CANCELLED)
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.state, TransferAlertState.ACTIVE)

    def test_the_source_school_cannot_cancel_someone_elses_request(self):
        with self.assertRaises(Rejected):
            verification.cancel_request(membership=self.owner, request_id=str(self.item.id))

    def test_cancelling_twice_is_refused(self):
        verification.cancel_request(membership=self.other_owner, request_id=str(self.item.id))
        with self.assertRaises(Rejected):
            verification.cancel_request(membership=self.other_owner, request_id=str(self.item.id))

    def test_a_new_request_can_be_sent_once_the_old_one_is_cancelled(self):
        verification.cancel_request(membership=self.other_owner, request_id=str(self.item.id))
        second = verification.send_request(membership=self.other_owner, transfer_alert_id=str(self.alert.id))
        self.assertNotEqual(second.id, self.item.id)


class ListingTests(VerificationTestCase):
    def test_each_school_sees_its_own_side_of_the_conversation(self):
        verification.send_request(membership=self.other_owner, transfer_alert_id=str(self.alert.id))
        sent = verification.requests_sent_by(self.other_school)
        received = verification.requests_received_by(self.school)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(received), 1)
        self.assertEqual(sent[0].id, received[0].id)
        self.assertEqual(verification.requests_sent_by(self.school), [])
        self.assertEqual(verification.requests_received_by(self.other_school), [])


class VerificationEndpointTests(VerificationTestCase):
    def test_the_full_round_trip_over_http(self):
        self.client.force_authenticate(self.other_owner.user)
        sent = self.client.post(
            f"/api/v1/schools/{self.other_school.id}/transferverify/network/requests/",
            {"transferAlertId": str(self.alert.id), "note": "Admitting this term."},
            format="json",
        )
        self.assertEqual(sent.status_code, 201)
        request_id = sent.json()["request"]["id"]

        mine = self.client.get(f"/api/v1/schools/{self.other_school.id}/transferverify/network/requests/sent/")
        self.assertEqual(len(mine.json()["requests"]), 1)

        self.client.force_authenticate(self.owner.user)
        received = self.client.get(f"/api/v1/schools/{self.school.id}/transferverify/network/requests/received/")
        self.assertEqual(len(received.json()["requests"]), 1)

        answered = self.client.post(
            f"/api/v1/schools/{self.school.id}/transferverify/network/requests/{request_id}/respond/",
            {"decision": "confirmed", "note": "Still unresolved."},
            format="json",
        )
        self.assertEqual(answered.status_code, 200)
        self.assertEqual(answered.json()["request"]["status"], "confirmed")

    def test_only_the_source_owner_can_reach_the_received_list(self):
        self.client.force_authenticate(self.members["accountant"].user)
        response = self.client.get(f"/api/v1/schools/{self.school.id}/transferverify/network/requests/received/")
        self.assertEqual(response.status_code, 403)
