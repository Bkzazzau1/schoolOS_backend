from apps.core.errors import Rejected

from .. import biometrics
from ..biometrics import MatchConfidence, evaluate_confidence
from ..models import StudentBiometricTemplate, StudentNetworkEnrollment
from .test_network import NetworkTestCase


class EvaluateConfidenceTests(NetworkTestCase):
    """Pure-function tests for the authority rule itself - no models
    involved, since this is the one thing Phase 5 must get right regardless
    of whether real capture hardware exists yet."""

    def test_neither_signal_is_no_match(self):
        self.assertEqual(evaluate_confidence(phone_matched=False, template_matched=None), MatchConfidence.NO_MATCH)
        self.assertEqual(evaluate_confidence(phone_matched=False, template_matched=False), MatchConfidence.NO_MATCH)

    def test_phone_alone_is_only_ever_a_candidate_match(self):
        self.assertEqual(evaluate_confidence(phone_matched=True, template_matched=None), MatchConfidence.CANDIDATE_MATCH)

    def test_template_alone_is_a_strong_match_not_confirmed(self):
        self.assertEqual(evaluate_confidence(phone_matched=False, template_matched=True), MatchConfidence.STRONG_MATCH)

    def test_phone_and_template_together_is_confirmed(self):
        self.assertEqual(evaluate_confidence(phone_matched=True, template_matched=True), MatchConfidence.CONFIRMED_MATCH)

    def test_a_conflicting_template_against_a_matching_phone_needs_manual_review(self):
        self.assertEqual(evaluate_confidence(phone_matched=True, template_matched=False), MatchConfidence.MANUAL_REVIEW_REQUIRED)


class ConsentTests(NetworkTestCase):
    def test_granting_consent_creates_one_active_record(self):
        consent = biometrics.grant_consent(membership=self.owner, student=self.student, guardian_name="Mrs Okafor")
        self.assertTrue(consent.is_active)
        self.assertEqual(consent.guardian_name, "Mrs Okafor")

    def test_granting_twice_returns_the_same_active_consent(self):
        first = biometrics.grant_consent(membership=self.owner, student=self.student, guardian_name="Mrs Okafor")
        second = biometrics.grant_consent(membership=self.owner, student=self.student, guardian_name="Mrs Okafor")
        self.assertEqual(first.id, second.id)

    def test_an_empty_guardian_name_is_refused(self):
        with self.assertRaises(Rejected):
            biometrics.grant_consent(membership=self.owner, student=self.student, guardian_name="   ")

    def test_withdrawing_with_nothing_active_is_refused(self):
        with self.assertRaises(Rejected):
            biometrics.withdraw_consent(membership=self.owner, student=self.student)

    def test_withdrawing_consent_revokes_every_active_template_under_it(self):
        biometrics.grant_consent(membership=self.owner, student=self.student, guardian_name="Mrs Okafor")
        template = biometrics.capture_template(
            membership=self.owner, student=self.student, finger="right_index",
            template_data=b"opaque-template-bytes", template_version="v1",
        )
        biometrics.withdraw_consent(membership=self.owner, student=self.student)
        template.refresh_from_db()
        self.assertIsNotNone(template.revoked_at)

        # A fresh consent afterwards lets a new template be captured again.
        biometrics.grant_consent(membership=self.owner, student=self.student, guardian_name="Mrs Okafor")
        second = biometrics.capture_template(
            membership=self.owner, student=self.student, finger="right_index",
            template_data=b"another-template", template_version="v1",
        )
        self.assertIsNone(second.revoked_at)


class CaptureTests(NetworkTestCase):
    def test_capturing_without_consent_is_refused(self):
        with self.assertRaises(Rejected):
            biometrics.capture_template(
                membership=self.owner, student=self.student, finger="right_index",
                template_data=b"bytes", template_version="v1",
            )

    def test_capturing_reuses_the_same_network_identity_phone_matching_uses(self):
        biometrics.grant_consent(membership=self.owner, student=self.student, guardian_name="Mrs Okafor")
        template = biometrics.capture_template(
            membership=self.owner, student=self.student, finger="right_index",
            template_data=b"bytes", template_version="v1",
        )
        enrollment = StudentNetworkEnrollment.objects.get(school=self.school, student=self.student)
        self.assertEqual(template.network_identity_id, enrollment.network_identity_id)

    def test_more_than_two_fingers_is_supported(self):
        biometrics.grant_consent(membership=self.owner, student=self.student, guardian_name="Mrs Okafor")
        for finger in ("right_thumb", "right_index", "right_middle"):
            biometrics.capture_template(
                membership=self.owner, student=self.student, finger=finger,
                template_data=f"bytes-{finger}".encode(), template_version="v1",
            )
        self.assertEqual(StudentBiometricTemplate.objects.filter(enrolled_school=self.school).count(), 3)

    def test_an_invalid_finger_is_refused(self):
        biometrics.grant_consent(membership=self.owner, student=self.student, guardian_name="Mrs Okafor")
        with self.assertRaises(Rejected):
            biometrics.capture_template(
                membership=self.owner, student=self.student, finger="tail",
                template_data=b"bytes", template_version="v1",
            )

    def test_revoking_a_template_directly_works_and_cannot_repeat(self):
        biometrics.grant_consent(membership=self.owner, student=self.student, guardian_name="Mrs Okafor")
        template = biometrics.capture_template(
            membership=self.owner, student=self.student, finger="right_index",
            template_data=b"bytes", template_version="v1",
        )
        revoked = biometrics.revoke_template(membership=self.owner, template_id=str(template.id))
        self.assertIsNotNone(revoked.revoked_at)
        with self.assertRaises(Rejected):
            biometrics.revoke_template(membership=self.owner, template_id=str(template.id))

    def test_a_template_cannot_be_revoked_from_another_school(self):
        biometrics.grant_consent(membership=self.owner, student=self.student, guardian_name="Mrs Okafor")
        template = biometrics.capture_template(
            membership=self.owner, student=self.student, finger="right_index",
            template_data=b"bytes", template_version="v1",
        )
        with self.assertRaises(Rejected):
            biometrics.revoke_template(membership=self.other_owner, template_id=str(template.id))


class MatchConfidenceForIdentityTests(NetworkTestCase):
    def setUp(self):
        super().setUp()
        biometrics.grant_consent(membership=self.owner, student=self.student, guardian_name="Mrs Okafor")
        self.template = biometrics.capture_template(
            membership=self.owner, student=self.student, finger="right_index",
            template_data=b"the-real-template", template_version="v1",
        )

    def test_a_matching_template_and_phone_together_confirm(self):
        confidence = biometrics.match_confidence_for_identity(
            network_identity=self.template.network_identity, phone_matched=True,
            candidate_finger="right_index", candidate_template_data=b"the-real-template",
        )
        self.assertEqual(confidence, MatchConfidence.CONFIRMED_MATCH)

    def test_a_non_matching_template_with_a_matching_phone_needs_manual_review(self):
        confidence = biometrics.match_confidence_for_identity(
            network_identity=self.template.network_identity, phone_matched=True,
            candidate_finger="right_index", candidate_template_data=b"a-different-template",
        )
        self.assertEqual(confidence, MatchConfidence.MANUAL_REVIEW_REQUIRED)

    def test_no_template_offered_falls_back_to_phone_only(self):
        confidence = biometrics.match_confidence_for_identity(
            network_identity=self.template.network_identity, phone_matched=True,
        )
        self.assertEqual(confidence, MatchConfidence.CANDIDATE_MATCH)

    def test_a_revoked_template_is_never_matched_against(self):
        biometrics.revoke_template(membership=self.owner, template_id=str(self.template.id))
        confidence = biometrics.match_confidence_for_identity(
            network_identity=self.template.network_identity, phone_matched=True,
            candidate_finger="right_index", candidate_template_data=b"the-real-template",
        )
        self.assertEqual(confidence, MatchConfidence.CANDIDATE_MATCH)
