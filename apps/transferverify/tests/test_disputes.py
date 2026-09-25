from apps.core.errors import Rejected
from apps.students.models import GuardianLink

from .. import disputes, services
from ..models import ClearanceStatus, DisputeStatus, TransferAlertState
from .test_verification import VerificationTestCase


class DisputeTestCase(VerificationTestCase):
    def setUp(self):
        super().setUp()
        self.guardian = self.members["parent"]
        # NetworkTestCase.setUp already recorded this student's guardian phone -
        # link the Parent membership's own account to that same real record.
        GuardianLink.objects.filter(student=self.student).update(account_user=self.guardian.user)


class OpeningTests(DisputeTestCase):
    def test_a_recorded_guardian_opens_a_dispute_and_the_alert_flips_to_disputed(self):
        item = disputes.open_dispute(
            membership=self.guardian, transfer_alert_id=str(self.alert.id), reason="already_paid",
            explanation="We paid in cash at the office.",
        )
        self.assertEqual(item.status, DisputeStatus.OPENED)
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.state, TransferAlertState.DISPUTED)

    def test_someone_who_is_not_a_recorded_guardian_cannot_dispute(self):
        other_user_membership = self.members["accountant"]
        with self.assertRaises(Rejected):
            disputes.open_dispute(membership=other_user_membership, transfer_alert_id=str(self.alert.id), reason="already_paid")

    def test_a_guardian_at_a_different_school_cannot_dispute(self):
        with self.assertRaises(Rejected):
            disputes.open_dispute(
                membership=self.other_principal, transfer_alert_id=str(self.alert.id), reason="already_paid"
            )

    def test_an_invalid_reason_is_refused(self):
        with self.assertRaises(Rejected):
            disputes.open_dispute(membership=self.guardian, transfer_alert_id=str(self.alert.id), reason="because")

    def test_a_second_open_dispute_for_the_same_case_is_refused(self):
        disputes.open_dispute(membership=self.guardian, transfer_alert_id=str(self.alert.id), reason="already_paid")
        with self.assertRaises(Rejected):
            disputes.open_dispute(membership=self.guardian, transfer_alert_id=str(self.alert.id), reason="amount_disputed")


class ReviewTests(DisputeTestCase):
    def setUp(self):
        super().setUp()
        self.dispute = disputes.open_dispute(
            membership=self.guardian, transfer_alert_id=str(self.alert.id), reason="already_paid"
        )

    def test_the_owner_accepts_and_the_alert_returns_to_active(self):
        reviewed = disputes.review_dispute(membership=self.owner, dispute_id=str(self.dispute.id), decision="accepted", note="Confirmed by receipt.")
        self.assertEqual(reviewed.status, DisputeStatus.ACCEPTED)
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.state, TransferAlertState.ACTIVE)

    def test_the_owner_rejects_with_a_note(self):
        reviewed = disputes.review_dispute(membership=self.owner, dispute_id=str(self.dispute.id), decision="rejected", note="No record of payment found.")
        self.assertEqual(reviewed.status, DisputeStatus.REJECTED)
        self.assertEqual(reviewed.resolution_note, "No record of payment found.")

    def test_only_the_owner_of_the_source_school_can_review(self):
        with self.assertRaises(Rejected):
            disputes.review_dispute(membership=self.members["accountant"], dispute_id=str(self.dispute.id), decision="accepted")
        with self.assertRaises(Rejected):
            disputes.review_dispute(membership=self.other_owner, dispute_id=str(self.dispute.id), decision="accepted")

    def test_reviewing_twice_is_refused(self):
        disputes.review_dispute(membership=self.owner, dispute_id=str(self.dispute.id), decision="accepted")
        with self.assertRaises(Rejected):
            disputes.review_dispute(membership=self.owner, dispute_id=str(self.dispute.id), decision="rejected")

    def test_an_invalid_decision_is_refused(self):
        with self.assertRaises(Rejected):
            disputes.review_dispute(membership=self.owner, dispute_id=str(self.dispute.id), decision="maybe")

    def test_disputes_for_school_and_opened_by_each_see_their_own_side(self):
        received = disputes.disputes_for_school(self.school)
        mine = disputes.disputes_opened_by(self.guardian)
        self.assertEqual(len(received), 1)
        self.assertEqual(len(mine), 1)
        self.assertEqual(received[0].id, mine[0].id)
        self.assertEqual(disputes.disputes_for_school(self.other_school), [])


class ClearanceTests(DisputeTestCase):
    def _resolve_case(self):
        services.resolve(membership=self.owner, external_id=self.published.external_id, note="Paid in full.")

    def test_issuing_requires_the_case_to_be_resolved_first(self):
        with self.assertRaises(Rejected):
            disputes.issue_clearance(membership=self.owner, external_id=self.published.external_id)
        self._resolve_case()
        clearance = disputes.issue_clearance(membership=self.owner, external_id=self.published.external_id)
        self.assertEqual(clearance.status, ClearanceStatus.ACTIVE)
        self.assertTrue(clearance.verification_token)

    def test_only_the_owner_can_issue_a_clearance(self):
        self._resolve_case()
        with self.assertRaises(Rejected):
            disputes.issue_clearance(membership=self.members["accountant"], external_id=self.published.external_id)

    def test_an_open_dispute_blocks_issuing_a_clearance(self):
        disputes.open_dispute(membership=self.guardian, transfer_alert_id=str(self.alert.id), reason="already_paid")
        self._resolve_case()
        with self.assertRaises(Rejected):
            disputes.issue_clearance(membership=self.owner, external_id=self.published.external_id)

    def test_a_second_active_clearance_for_the_same_case_is_refused(self):
        self._resolve_case()
        disputes.issue_clearance(membership=self.owner, external_id=self.published.external_id)
        with self.assertRaises(Rejected):
            disputes.issue_clearance(membership=self.owner, external_id=self.published.external_id)

    def test_revoking_and_reissuing_works(self):
        self._resolve_case()
        first = disputes.issue_clearance(membership=self.owner, external_id=self.published.external_id)
        disputes.revoke_clearance(membership=self.owner, clearance_id=str(first.id))
        second = disputes.issue_clearance(membership=self.owner, external_id=self.published.external_id)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(disputes.clearances_for_school(self.school), [second, first])

    def test_revoking_someone_elses_clearance_is_refused(self):
        self._resolve_case()
        clearance = disputes.issue_clearance(membership=self.owner, external_id=self.published.external_id)
        with self.assertRaises(Rejected):
            disputes.revoke_clearance(membership=self.other_owner, clearance_id=str(clearance.id))


class VerifyClearanceTests(DisputeTestCase):
    def test_a_valid_token_reports_valid_with_the_minimal_message_only(self):
        services.resolve(membership=self.owner, external_id=self.published.external_id, note="Paid in full.")
        clearance = disputes.issue_clearance(membership=self.owner, external_id=self.published.external_id)
        result = disputes.verify_clearance(clearance.verification_token)
        self.assertEqual(result, {"valid": True, "message": "Valid SchoolOS Transfer Clearance."})

    def test_a_revoked_token_is_not_valid(self):
        services.resolve(membership=self.owner, external_id=self.published.external_id, note="Paid in full.")
        clearance = disputes.issue_clearance(membership=self.owner, external_id=self.published.external_id)
        disputes.revoke_clearance(membership=self.owner, clearance_id=str(clearance.id))
        result = disputes.verify_clearance(clearance.verification_token)
        self.assertFalse(result["valid"])

    def test_an_unknown_or_empty_token_is_not_valid(self):
        self.assertFalse(disputes.verify_clearance("not-a-real-token")["valid"])
        self.assertFalse(disputes.verify_clearance("")["valid"])

    def test_the_response_never_carries_amounts_or_identifying_detail(self):
        services.resolve(membership=self.owner, external_id=self.published.external_id, note="Paid in full.")
        clearance = disputes.issue_clearance(membership=self.owner, external_id=self.published.external_id)
        result = disputes.verify_clearance(clearance.verification_token)
        self.assertEqual(set(result.keys()), {"valid", "message"})


class DisputeAndClearanceEndpointTests(DisputeTestCase):
    def test_dispute_review_resolve_issue_and_public_verify_over_http(self):
        self.client.force_authenticate(self.guardian.user)
        opened = self.client.post(
            f"/api/v1/schools/{self.school.id}/transferverify/network/disputes/",
            {"transferAlertId": str(self.alert.id), "reason": "already_paid", "explanation": "Paid in cash."},
            format="json",
        )
        self.assertEqual(opened.status_code, 201)
        dispute_id = opened.json()["dispute"]["id"]

        mine = self.client.get(f"/api/v1/schools/{self.school.id}/transferverify/network/disputes/mine/")
        self.assertEqual(len(mine.json()["disputes"]), 1)

        self.client.force_authenticate(self.owner.user)
        received = self.client.get(f"/api/v1/schools/{self.school.id}/transferverify/network/disputes/received/")
        self.assertEqual(len(received.json()["disputes"]), 1)

        reviewed = self.client.post(
            f"/api/v1/schools/{self.school.id}/transferverify/network/disputes/{dispute_id}/review/",
            {"decision": "rejected", "note": "No record found."},
            format="json",
        )
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(reviewed.json()["dispute"]["status"], "rejected")

        services.resolve(membership=self.owner, external_id=self.published.external_id, note="Paid in full.")
        issued = self.client.post(
            f"/api/v1/schools/{self.school.id}/transferverify/network/clearances/",
            {"externalId": self.published.external_id},
            format="json",
        )
        self.assertEqual(issued.status_code, 201)
        token = issued.json()["clearance"]["verificationToken"]

        listed = self.client.get(f"/api/v1/schools/{self.school.id}/transferverify/network/clearances/list/")
        self.assertEqual(len(listed.json()["clearances"]), 1)

        self.client.force_authenticate(user=None)
        verified = self.client.get(f"/api/v1/transferverify/clearances/verify/?token={token}")
        self.assertEqual(verified.status_code, 200)
        self.assertTrue(verified.json()["valid"])

        bogus = self.client.get("/api/v1/transferverify/clearances/verify/?token=not-real")
        self.assertFalse(bogus.json()["valid"])

    def test_only_a_parent_can_open_a_dispute_over_http(self):
        self.client.force_authenticate(self.members["accountant"].user)
        response = self.client.post(
            f"/api/v1/schools/{self.school.id}/transferverify/network/disputes/",
            {"transferAlertId": str(self.alert.id), "reason": "already_paid"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
