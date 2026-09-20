from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

from apps.invitations import service
from apps.invitations.models import InvitationEvent, StaffInvitation, StaffLink
from apps.invitations.throttles import AcceptThrottle, PreviewThrottle
from apps.notifications.models import Notification
from apps.schools.models import Membership
from apps.sync.models import SyncRecord

from .helpers import InviteTestCase

User = get_user_model()
INVALID = {"code": "invitation_invalid", "message": "This link is not valid."}


class PreviewTests(InviteTestCase):
    def setUp(self):
        super().setUp()
        self.staff_id, self.token = self.staff_with_link(name="Musa Ibrahim", email="musa@school.ng")

    def test_a_valid_link_says_what_it_is_for_and_shows_only_that(self):
        response = self.preview(self.token)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"schoolName", "staffName", "email", "expiresAt", "accountExists"})
        self.assertEqual((body["schoolName"], body["staffName"], body["email"]), ("BrightGate", "Musa Ibrahim", "m***@school.ng"))
        self.assertFalse(body["accountExists"])
        self.assertIn("previewed", list(self.pending(self.staff_id).events.values_list("event", flat=True)))

    def test_it_says_when_an_account_already_exists_for_that_email(self):
        User.objects.create_user("musa@school.ng", "an-existing-password-1")
        self.assertTrue(self.preview(self.token).json()["accountExists"])

    def test_no_sign_in_is_needed_and_a_bad_token_header_is_ignored(self):
        self.client.force_authenticate(None)
        response = self.client.get(f"/api/v1/invitations/{self.token}/", HTTP_AUTHORIZATION="Bearer garbage")
        self.assertEqual(response.status_code, 200)

    def test_every_kind_of_bad_link_gives_the_same_404(self):
        # Unknown, expired, revoked and replaced links cannot be told apart.
        invitation = self.pending(self.staff_id)
        answers = [self.preview("not-a-real-token")]
        invitation.expires_at = timezone.now() - timedelta(seconds=1)
        invitation.save()
        answers.append(self.preview(self.token))
        invitation.expires_at = timezone.now() + timedelta(days=1)
        invitation.status = "revoked"
        invitation.save()
        answers.append(self.preview(self.token))
        replaced_token = self.new_link(self.staff_id)
        answers.append(self.preview(self.token))   # the old link after a new one was sent
        for response in answers:
            self.assertEqual((response.status_code, response.json()), (404, INVALID))
        self.assertEqual(self.preview(replaced_token).status_code, 200)

    def test_a_link_that_was_already_used_says_so(self):
        self.accept(self.token, self.new_person_body())
        response = self.preview(self.token)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "already_accepted"))

    def test_a_link_only_works_on_its_own_schools_web_address(self):
        other = self.other_school
        self.assertTrue(other.domains.filter(host="other.schoolos.ng").exists())
        self.assertEqual(self.preview(self.token, host="brightgate.schoolos.ng").status_code, 200)
        self.assertEqual(self.preview(self.token, host="api.schoolos.ng").status_code, 200)      # the API host: no school
        wrong = self.preview(self.token, host="other.schoolos.ng")
        self.assertEqual((wrong.status_code, wrong.json()), (404, INVALID))
        self.assertEqual(self.accept(self.token, self.new_person_body(), host="other.schoolos.ng").status_code, 404)

    def test_asking_too_often_is_refused(self):
        cache.clear()
        with mock.patch.object(PreviewThrottle, "THROTTLE_RATES", {"invite_preview": "2/min"}):
            codes = [self.preview(self.token).status_code for _ in range(4)]
        cache.clear()
        self.assertEqual(codes, [200, 200, 429, 429])


class AcceptNewAccountTests(InviteTestCase):
    def setUp(self):
        super().setUp()
        self.staff_id, self.token = self.staff_with_link(name="Musa Ibrahim", email="musa@school.ng", systemRole="teacher")

    def test_accepting_creates_the_login_the_role_and_the_link_together(self):
        response = self.accept(self.token, self.new_person_body())
        self.assertEqual(response.status_code, 200, response.json())
        body = response.json()
        user = User.objects.get(email="musa@school.ng")
        self.assertTrue(user.check_password("a-good-long-password-1"))
        self.assertEqual((user.first_name, user.last_name), ("Musa", "Ibrahim"))
        membership = Membership.objects.get(user=user, school=self.school)
        self.assertEqual((membership.role, membership.is_active), ("teacher", True))
        self.assertEqual(body["membership"], {"id": str(membership.id), "schoolId": str(self.school.id),
                                              "schoolName": "BrightGate", "role": "teacher"})
        self.assertEqual(body["staffId"], self.staff_id)
        self.assertEqual(StaffLink.objects.get(staff_id=self.staff_id).membership, membership)
        # The staff record now says whose it is, and the version moved so devices pick it up.
        profile = self.stored("owner_staff_profile", self.staff_id)
        self.assertEqual(profile.payload["linkedMembershipId"], str(membership.id))
        self.assertGreater(profile.version, 1)
        invitation = StaffInvitation.objects.get(staff_id=self.staff_id)
        self.assertEqual((invitation.status, invitation.accepted_membership), ("accepted", membership))
        self.assertIsNotNone(invitation.accepted_at)
        self.assertIn("accepted", list(invitation.events.values_list("event", flat=True)))

    def test_the_tokens_returned_really_sign_the_person_in(self):
        body = self.accept(self.token, self.new_person_body()).json()
        self.client.force_authenticate(None)
        me = self.client.get("/api/v1/me/", HTTP_AUTHORIZATION=f"Bearer {body['access']}").json()
        self.assertEqual(me["email"], "musa@school.ng")
        self.assertEqual(me["memberships"][0]["role"], "teacher")
        refreshed = self.client.post("/api/v1/auth/token/refresh/", {"refresh": body["refresh"]}, format="json")
        self.assertEqual(refreshed.status_code, 200)
        signed_in = self.client.post("/api/v1/auth/token/", {"email": "musa@school.ng", "password": "a-good-long-password-1"}, format="json")
        self.assertEqual(signed_in.status_code, 200)   # the password they set works next time

    def test_the_account_uses_the_invitations_email_and_role_never_the_requests(self):
        response = self.accept(self.token, self.new_person_body(email="attacker@evil.ng", role="proprietor",
                                                                 systemRole="principal", schoolId=str(self.other_school.id)))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="attacker@evil.ng").exists())
        self.assertEqual(Membership.objects.get(user__email="musa@school.ng", school=self.school).role, "teacher")
        self.assertFalse(Membership.objects.filter(user__email="musa@school.ng", school=self.other_school).exists())

    def test_the_owner_is_told(self):
        self.accept(self.token, self.new_person_body())
        message = Notification.objects.get(recipient=self.owner, kind="staff_invitation_accepted")
        self.assertIn("Musa Ibrahim accepted their invitation", message.message)

    def test_a_weak_password_changes_nothing(self):
        for password in ["short", "1234567890", "password123", "musa@school.ng"]:
            response = self.accept(self.token, self.new_person_body(password=password))
            self.assertEqual(response.status_code, 400, password)
            self.assertEqual(response.json()["code"], "invalid_password")
            self.assertTrue(response.json()["details"])
        self.assertFalse(User.objects.filter(email="musa@school.ng").exists())
        self.assertEqual(StaffLink.objects.count(), 0)
        self.assertEqual(self.pending(self.staff_id).status, "pending")
        self.assertEqual(self.profile(self.staff_id)["linkedMembershipId"], "")
        # And the same link still works with a good one.
        self.assertEqual(self.accept(self.token, self.new_person_body()).status_code, 200)

    def test_using_the_link_again_is_refused_and_gives_no_tokens(self):
        self.assertEqual(self.accept(self.token, self.new_person_body()).status_code, 200)
        again = self.accept(self.token, self.new_person_body())
        self.assertEqual((again.status_code, again.json()["code"]), (409, "already_accepted"))
        self.assertNotIn("access", again.json())
        self.assertEqual(User.objects.filter(email="musa@school.ng").count(), 1)

    def test_bad_expired_and_replaced_links_are_refused_the_same_way(self):
        self.assertEqual(self.accept("nope", self.new_person_body()).json(), INVALID)
        invitation = self.pending(self.staff_id)
        invitation.expires_at = timezone.now() - timedelta(seconds=1)
        invitation.save()
        self.assertEqual(self.accept(self.token, self.new_person_body()).status_code, 404)
        self.assertFalse(User.objects.filter(email="musa@school.ng").exists())

    def test_the_staff_record_being_gone_makes_the_link_invalid_and_creates_nothing(self):
        SyncRecord.objects.filter(entity_type="owner_staff_profile").delete()
        self.assertEqual(self.accept(self.token, self.new_person_body()).status_code, 404)
        self.assertFalse(User.objects.filter(email="musa@school.ng").exists())

    def test_trying_too_often_is_refused(self):
        cache.clear()
        with mock.patch.object(AcceptThrottle, "THROTTLE_RATES", {"invite_accept": "2/min"}):
            codes = [self.accept(self.token, self.new_person_body(password="short")).status_code for _ in range(4)]
        cache.clear()
        self.assertEqual(codes, [400, 400, 429, 429])


class AcceptExistingAccountTests(InviteTestCase):
    def setUp(self):
        super().setUp()
        self.staff_id, self.token = self.staff_with_link(email="musa@school.ng", systemRole="teacher")
        self.user = User.objects.create_user("musa@school.ng", "my-existing-password-1")

    def test_an_existing_account_must_sign_in_and_the_link_alone_is_not_enough(self):
        response = self.accept(self.token, self.new_person_body())
        self.assertEqual((response.status_code, response.json()["code"]), (401, "sign_in_required"))
        self.assertFalse(Membership.objects.filter(user=self.user, school=self.school).exists())
        self.assertEqual(StaffLink.objects.count(), 0)
        self.assertTrue(User.objects.get(email="musa@school.ng").check_password("my-existing-password-1"))

    def test_signed_in_as_that_account_it_works_without_a_password(self):
        response = self.accept(self.token, {}, user=self.user)
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["membership"]["role"], "teacher")
        self.assertTrue(User.objects.get(email="musa@school.ng").check_password("my-existing-password-1"))  # untouched
        self.assertEqual(StaffLink.objects.get().membership.user, self.user)

    def test_signed_in_as_someone_else_is_refused(self):
        stranger = User.objects.create_user("stranger@x.ng", "another-password-123")
        response = self.accept(self.token, {}, user=stranger)
        self.assertEqual((response.status_code, response.json()["code"]), (403, "wrong_account"))
        self.assertFalse(Membership.objects.filter(user=stranger, school=self.school).exists())
        self.assertEqual(StaffLink.objects.count(), 0)

    def test_the_email_match_ignores_case(self):
        shouty = User.objects.create_user("SHOUTY@x.ng", "another-password-123")
        service.create_invitation(self.school, self.staff_id, "shouty@x.ng", "teacher")
        token = self.new_link(self.staff_id, email="shouty@x.ng", role="teacher")
        self.assertEqual(self.accept(token, {}, user=shouty).status_code, 200)

    def test_someone_already_a_parent_here_gets_a_separate_staff_membership(self):
        Membership.objects.create(user=self.user, school=self.school, role="parent")
        response = self.accept(self.token, {}, user=self.user)
        self.assertEqual(response.status_code, 200)
        roles = set(Membership.objects.filter(user=self.user, school=self.school).values_list("role", flat=True))
        self.assertEqual(roles, {"parent", "teacher"})


class LinkingRulesTests(InviteTestCase):
    def setUp(self):
        super().setUp()
        self.staff_id, self.token = self.staff_with_link(email="musa@school.ng", systemRole="teacher")

    def test_a_staff_record_that_is_already_linked_cannot_be_claimed_again(self):
        StaffLink.objects.create(school=self.school, staff_id=self.staff_id, membership=self.members["staff"])
        response = self.accept(self.token, self.new_person_body())
        self.assertEqual((response.status_code, response.json()["code"]), (409, "already_linked"))
        # Nothing was left behind by the attempt.
        self.assertFalse(User.objects.filter(email="musa@school.ng").exists())
        self.assertEqual(self.pending(self.staff_id).status, "pending")

    def test_a_login_cannot_be_two_staff_members(self):
        other_staff, other_token = self.staff_with_link(email="musa@school.ng", systemRole="teacher", name="Second Person")
        # Same email, same role: the first acceptance links; the second must not.
        first = self.accept(other_token, self.new_person_body())
        self.assertEqual(first.status_code, 200)
        user = User.objects.get(email="musa@school.ng")
        token = self.new_link(self.staff_id, email="musa@school.ng", role="teacher")
        second = self.accept(token, {}, user=user)
        self.assertEqual((second.status_code, second.json()["code"]), (409, "already_linked"))
        self.assertEqual(StaffLink.objects.filter(membership__user=user).count(), 1)

    def test_authority_and_jobs_given_before_the_login_become_active_and_tied_to_it(self):
        def rec(entity_type, entity_id, payload):
            SyncRecord.objects.create(school=self.school, entity_type=entity_type, entity_id=entity_id, payload=payload)

        rec("owner_payroll_authorizer", self.staff_id, {"staffId": self.staff_id, "authorities": ["approveStaff"],
                                                        "status": "pendingActivation", "membershipId": None})
        rec("owner_job_assignment", "JOB-1", {"registeredStaffId": self.staff_id, "role": "sectionHead", "status": "pendingActivation", "membershipId": None})
        rec("owner_job_assignment", "JOB-REVOKED", {"registeredStaffId": self.staff_id, "status": "revoked", "membershipId": None})
        rec("owner_job_assignment", "JOB-OTHER", {"registeredStaffId": "STAFF-SOMEONE-ELSE", "status": "pendingActivation", "membershipId": None})
        rec("owner_payroll_authorizer", "STAFF-OTHER", {"staffId": "STAFF-OTHER", "status": "pendingActivation"})

        body = self.accept(self.token, self.new_person_body()).json()
        membership_id = body["membership"]["id"]
        auth = self.stored("owner_payroll_authorizer", self.staff_id)
        self.assertEqual((auth.payload["status"], auth.payload["membershipId"]), ("active", membership_id))
        self.assertGreater(auth.version, 1)
        job = self.stored("owner_job_assignment", "JOB-1").payload
        self.assertEqual((job["status"], job["membershipId"]), ("active", membership_id))
        # Only this person's pending assignments: revoked and other people's are left alone.
        self.assertEqual(self.stored("owner_job_assignment", "JOB-REVOKED").payload["status"], "revoked")
        self.assertEqual(self.stored("owner_job_assignment", "JOB-OTHER").payload["status"], "pendingActivation")
        self.assertEqual(self.stored("owner_payroll_authorizer", "STAFF-OTHER").payload["status"], "pendingActivation")

    def test_an_assigned_approver_can_act_the_moment_they_link(self):
        # The owner assigned "approve staff" to this person before they had a login.
        SyncRecord.objects.create(school=self.school, entity_type="owner_payroll_authorizer", entity_id=self.staff_id,
                                  payload={"staffId": self.staff_id, "authorities": ["approveStaff"],
                                           "status": "pendingActivation", "membershipId": None})
        self.ok(self.propose(proposal_id="P-NEXT"))
        before = self.decide("approve", None, "P-NEXT")
        self.assertEqual(before.status_code, 401)
        body = self.accept(self.token, self.new_person_body()).json()
        user = User.objects.get(email="musa@school.ng")
        self.client.force_authenticate(user)
        approved = self.client.post(f"/api/v1/staff/schools/{self.school.id}/proposals/P-NEXT/approve/", {}, format="json")
        self.assertEqual(approved.status_code, 200, approved.json())

    def test_events_never_contain_the_link(self):
        self.preview(self.token)
        self.accept(self.token, self.new_person_body())
        for event in InvitationEvent.objects.all():
            self.assertNotIn(self.token, str(event.detail) + event.user_agent + event.event)
