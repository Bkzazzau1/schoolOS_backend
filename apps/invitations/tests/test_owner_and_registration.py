from django.contrib.auth import get_user_model
from django.core import mail

from apps.invitations.models import StaffInvitation, StaffLink
from apps.notifications.models import Notification
from apps.schools.models import Membership
from apps.staff.tests.helpers import fresh_nin, fresh_phone
from apps.sync.models import SyncRecord

from .helpers import InviteTestCase

User = get_user_model()


def details(**over):
    body = {
        "phone": fresh_phone(), "nin": fresh_nin(), "address": "12 Market Road, Kano",
        "dateOfBirth": "1990-05-14", "gender": "male", "stateOfOrigin": "Kano",
        "nextOfKinName": "Aisha Ibrahim", "nextOfKinPhone": fresh_phone(),
    }
    body.update(over)
    return body


BANK = {"bankName": "Access Bank", "accountName": "Musa Ibrahim", "accountNumber": "0123456789"}


class OwnerInvitationEndpointTests(InviteTestCase):
    def setUp(self):
        super().setUp()
        self.staff_id, self.token = self.staff_with_link(email="musa@school.ng", systemRole="teacher")
        self.url = f"/api/v1/owner/schools/{self.school.id}/staff/{self.staff_id}/invitation/"

    def call(self, method, who, body=None, url=None):
        self.client.force_authenticate(who.user)
        return getattr(self.client, method)(url or self.url, body or {}, format="json")

    def test_owner_principal_and_administrator_can_see_where_it_stands(self):
        for role in ("proprietor", "principal", "administrator"):
            response = self.call("get", self.members[role])
            self.assertEqual(response.status_code, 200, role)
            self.assertEqual(response.json()["status"], "pending")
            self.assertNotIn(self.token, str(response.json()))

    def test_other_roles_and_other_schools_cannot(self):
        for who in (self.members["teacher"], self.members["parent"], self.members["staff"], self.other_owner):
            self.assertIn(self.call("get", who).status_code, (403, 404), who.role)
            self.assertIn(self.call("post", who).status_code, (403, 404), who.role)
            self.assertIn(self.call("delete", who).status_code, (403, 404), who.role)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_sending_again_replaces_the_link_and_can_fix_the_email(self):
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.call("post", self.members["principal"], {"email": "Musa.Fixed@school.ng"})
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["email"], "musa.fixed@school.ng")
        self.assertEqual(mail.outbox[-1].to, ["musa.fixed@school.ng"])
        self.assertEqual(self.preview(self.token).status_code, 404)                 # the old link is dead
        self.assertEqual(self.preview(self.token_from_mail()).status_code, 200)
        self.assertEqual(self.profile(self.staff_id)["onboardingEmail"], "musa.fixed@school.ng")
        self.assertEqual(StaffInvitation.objects.filter(staff_id=self.staff_id, status="pending").count(), 1)

    def test_resending_after_the_request_was_submitted_reopens_it(self):
        payload = self.profile(self.staff_id)
        payload["onboardingStatus"] = "submitted"
        record = self.stored("owner_staff_profile", self.staff_id)
        record.payload = payload
        record.save()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.call("post", self.owner).status_code, 200)
        self.assertEqual(self.profile(self.staff_id)["onboardingStatus"], "invitePending")

    def test_a_bad_email_or_unknown_staff_is_refused(self):
        self.assertEqual(self.call("post", self.owner, {"email": "nope"}).status_code, 400)
        missing = f"/api/v1/owner/schools/{self.school.id}/staff/STAFF-NOPE/invitation/"
        self.assertEqual(self.call("post", self.owner, url=missing).json()["code"], "not_found")

    def test_only_the_owner_can_cancel_it(self):
        self.assertEqual(self.call("delete", self.members["principal"]).status_code, 403)
        self.assertEqual(self.call("delete", self.owner).status_code, 204)
        self.assertEqual(self.preview(self.token).status_code, 404)
        self.assertEqual(self.call("delete", self.owner).status_code, 404)          # nothing left to cancel

    def test_nothing_is_sent_once_the_person_has_a_login(self):
        self.accept(self.token, self.new_person_body())
        response = self.call("post", self.owner)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "already_linked"))


class UnlinkTests(InviteTestCase):
    def setUp(self):
        super().setUp()
        self.staff_id, token = self.staff_with_link(email="musa@school.ng", systemRole="teacher")
        SyncRecord.objects.create(school=self.school, entity_type="owner_payroll_authorizer", entity_id=self.staff_id,
                                  payload={"staffId": self.staff_id, "authorities": ["approveStaff"],
                                           "status": "pendingActivation", "membershipId": None})
        self.accept(token, self.new_person_body())
        self.user = User.objects.get(email="musa@school.ng")
        self.url = f"/api/v1/owner/schools/{self.school.id}/staff/{self.staff_id}/unlink/"

    def test_unlinking_ends_their_staff_access_and_authority_but_keeps_the_account(self):
        self.client.force_authenticate(self.owner.user)
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 200)
        self.assertFalse(StaffLink.objects.exists())
        self.assertEqual(self.profile(self.staff_id)["linkedMembershipId"], "")
        self.assertEqual(self.stored("owner_payroll_authorizer", self.staff_id).payload["status"], "revoked")
        self.assertFalse(Membership.objects.get(user=self.user, school=self.school).is_active)
        self.assertTrue(User.objects.filter(email="musa@school.ng").exists())
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(f"/api/v1/staff/schools/{self.school.id}/proposals/x/").status_code in (403, 404, 405), True)

    def test_only_the_owner_may_unlink_and_only_when_linked(self):
        self.client.force_authenticate(self.members["principal"].user)
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 403)
        self.client.force_authenticate(self.owner.user)
        self.client.post(self.url, {}, format="json")
        self.assertEqual(self.client.post(self.url, {}, format="json").json()["code"], "not_linked")

    def test_after_unlinking_a_new_invitation_can_link_someone_else(self):
        self.client.force_authenticate(self.owner.user)
        self.client.post(self.url, {}, format="json")
        token = self.new_link(self.staff_id, email="replacement@school.ng", role="teacher")
        self.assertEqual(self.accept(token, self.new_person_body(firstName="Sani", lastName="Bello")).status_code, 200)
        self.assertEqual(StaffLink.objects.get().membership.user.email, "replacement@school.ng")


class OnboardingEndpointTests(InviteTestCase):
    """staff/me/onboarding/: the linked person's own registration, from the app after signing in."""

    def setUp(self):
        super().setUp()
        self.staff_id, token = self.staff_with_link(email="musa@school.ng", systemRole="teacher", name="Musa Ibrahim")
        self.accept(token, self.new_person_body())
        self.user = User.objects.get(email="musa@school.ng")
        self.client.force_authenticate(self.user)

    def test_it_shows_what_is_asked_and_nothing_private_about_pay(self):
        response = self.client.get("/api/v1/staff/me/onboarding/")
        self.assertEqual(response.status_code, 200, response.json())
        body = response.json()
        self.assertEqual((body["staffId"], body["schoolName"], body["status"]), (self.staff_id, "BrightGate", "invitePending"))
        self.assertNotIn("gross", str(body))

    def test_submitting_fills_the_record_and_hands_it_to_the_school(self):
        response = self.client.post("/api/v1/staff/me/onboarding/", {"personal": details(), "payment": BANK}, format="json")
        self.assertEqual(response.status_code, 200, response.json())
        stored = self.profile(self.staff_id)
        self.assertEqual(stored["onboardingStatus"], "submitted")
        self.assertEqual(stored["payment"]["accountNumber"], "0123456789")
        self.assertEqual(stored["personal"]["address"], "12 Market Road, Kano")
        # It is closed now.
        self.assertEqual(self.client.get("/api/v1/staff/me/onboarding/").json()["code"], "no_open_request")

    def test_the_school_controlled_terms_cannot_be_changed_from_here(self):
        before = self.profile(self.staff_id)["personal"]
        self.client.post("/api/v1/staff/me/onboarding/",
                         {"personal": details(employmentType="permanent", employmentDate="2001-01-01"), "payment": BANK}, format="json")
        after = self.profile(self.staff_id)["personal"]
        self.assertEqual((after["employmentType"], after["employmentDate"]), (before["employmentType"], before["employmentDate"]))

    def test_bad_details_are_refused_and_nothing_is_saved(self):
        for personal, payment in [(details(nin="123"), BANK), (details(phone="abc"), BANK),
                                  (details(), {**BANK, "accountNumber": "12"})]:
            response = self.client.post("/api/v1/staff/me/onboarding/", {"personal": personal, "payment": payment}, format="json")
            self.assertEqual(response.status_code, 400, response.json())
        self.assertEqual(self.profile(self.staff_id)["onboardingStatus"], "invitePending")

    def test_a_phone_or_nin_that_belongs_to_someone_else_is_a_duplicate(self):
        other_id = self.make_staff(phone="08039990001", nin="99999999991")
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/v1/staff/me/onboarding/",
                                    {"personal": details(phone="08039990001"), "payment": BANK}, format="json")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "duplicate_identity"))
        self.assertEqual(self.profile(self.staff_id)["onboardingStatus"], "invitePending")
        self.assertNotEqual(other_id, self.staff_id)

    def test_someone_without_a_request_or_not_signed_in_gets_nothing(self):
        self.client.force_authenticate(self.members["teacher"].user)
        self.assertEqual(self.client.get("/api/v1/staff/me/onboarding/").status_code, 404)
        self.assertEqual(self.client.post("/api/v1/staff/me/onboarding/", {"personal": {}, "payment": {}}, format="json").status_code, 404)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/v1/staff/me/onboarding/").status_code, 401)

    def test_a_person_who_holds_several_roles_names_which_one(self):
        Membership.objects.create(user=self.user, school=self.other_school, role="parent")
        membership = Membership.objects.get(user=self.user, school=self.school, role="teacher")
        self.assertEqual(self.client.get(f"/api/v1/staff/me/onboarding/?membership={membership.id}").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/staff/me/onboarding/?membership=not-a-uuid").status_code, 404)


class WebPageTests(InviteTestCase):
    def setUp(self):
        super().setUp()
        self.staff_id, self.token = self.staff_with_link(email="musa@school.ng", systemRole="teacher", name="Musa Ibrahim")
        self.host = {"HTTP_HOST": "brightgate.schoolos.ng"}
        self.page = f"/invite/{self.token}/"

    def test_the_page_shows_the_school_and_person_and_is_private(self):
        response = self.client.get(self.page, **self.host)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "BrightGate")
        self.assertContains(response, "Musa Ibrahim")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertIn("no-store", response["Cache-Control"])

    def test_a_bad_link_shows_a_plain_page_with_a_404(self):
        self.assertEqual(self.client.get("/invite/nope/", **self.host).status_code, 404)
        self.assertEqual(self.client.get(self.page, HTTP_HOST="other.schoolos.ng").status_code, 404)

    def test_accepting_on_the_web_signs_in_and_goes_on_to_the_form(self):
        response = self.client.post(self.page, {"password": "a-good-long-password-1", "password2": "a-good-long-password-1",
                                                "first_name": "Musa", "last_name": "Ibrahim"}, **self.host)
        self.assertEqual((response.status_code, response["Location"]), (302, "/invite/registration/"))
        self.assertTrue(StaffLink.objects.exists())
        form = self.client.get("/invite/registration/", **self.host)
        self.assertEqual(form.status_code, 200)
        self.assertContains(form, "Account number")

    def test_mismatched_or_weak_passwords_are_explained_and_change_nothing(self):
        mismatch = self.client.post(self.page, {"password": "a-good-long-password-1", "password2": "different-password-9"}, **self.host)
        self.assertContains(mismatch, "do not match", status_code=400)
        weak = self.client.post(self.page, {"password": "12345678", "password2": "12345678"}, **self.host)
        self.assertEqual(weak.status_code, 400)
        self.assertFalse(User.objects.filter(email="musa@school.ng").exists())

    def test_an_existing_account_must_give_its_password(self):
        User.objects.create_user("musa@school.ng", "my-existing-password-1")
        wrong = self.client.post(self.page, {"password": "not-my-password-1"}, **self.host)
        self.assertContains(wrong, "not correct", status_code=400)
        self.assertFalse(StaffLink.objects.exists())
        right = self.client.post(self.page, {"password": "my-existing-password-1"}, **self.host)
        self.assertEqual(right.status_code, 302)
        self.assertTrue(StaffLink.objects.exists())

    def test_too_many_wrong_attempts_lock_the_link_for_a_while(self):
        from django.core.cache import cache

        cache.clear()
        User.objects.create_user("musa@school.ng", "my-existing-password-1")
        codes = [self.client.post(self.page, {"password": "wrong-password-1"}, **self.host).status_code for _ in range(12)]
        cache.clear()
        self.assertEqual(codes[:10], [400] * 10)
        self.assertEqual(codes[10:], [429, 429])

    def test_a_used_link_says_so(self):
        self.client.post(self.page, {"password": "a-good-long-password-1", "password2": "a-good-long-password-1"}, **self.host)
        self.client.logout()
        self.assertContains(self.client.get(self.page, **self.host), "already used", status_code=409)

    def test_the_form_needs_the_person_to_have_accepted_first(self):
        self.assertEqual(self.client.get("/invite/registration/", **self.host).status_code, 403)

    def test_filling_the_form_submits_the_registration(self):
        self.client.post(self.page, {"password": "a-good-long-password-1", "password2": "a-good-long-password-1",
                                     "first_name": "Musa", "last_name": "Ibrahim"}, **self.host)
        response = self.client.post("/invite/registration/", {**details(), **BANK}, **self.host)
        self.assertEqual(response.status_code, 200, response.content.decode()[:400])
        self.assertEqual(self.profile(self.staff_id)["onboardingStatus"], "submitted")
        self.assertEqual(self.profile(self.staff_id)["payment"]["accountNumber"], "0123456789")
        after = self.client.get("/invite/registration/", **self.host)
        self.assertContains(after, "Nothing")

    def test_the_web_form_applies_the_same_rules_as_the_app(self):
        self.client.post(self.page, {"password": "a-good-long-password-1", "password2": "a-good-long-password-1"}, **self.host)
        bad = self.client.post("/invite/registration/", {**details(nin="123"), **BANK}, **self.host)
        self.assertEqual(bad.status_code, 400)
        self.make_staff(phone="08039990002", nin="99999999992")
        duplicate = self.client.post("/invite/registration/", {**details(phone="08039990002"), **BANK}, **self.host)
        self.assertContains(duplicate, "contact the school office", status_code=409)
        self.assertEqual(self.profile(self.staff_id)["onboardingStatus"], "invitePending")


class EndToEndTests(InviteTestCase):
    def test_from_proposal_to_a_registered_staff_member(self):
        """The whole journey: proposed, approved, emailed, accepted, registered, owner told."""
        mail.outbox.clear()
        self.ok(self.propose(name="Zainab Musa", systemRole="teacher", email="zainab@school.ng"))
        with self.captureOnCommitCallbacks(execute=True):
            approved = self.approve(self.owner, self.last_proposal_id)
        staff_id = approved.json()["staffId"]
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["zainab@school.ng"])

        token = self.token_from_mail()
        accepted = self.accept(token, self.new_person_body(firstName="Zainab", lastName="Musa"))
        self.assertEqual(accepted.status_code, 200, accepted.json())
        user = User.objects.get(email="zainab@school.ng")

        self.client.force_authenticate(user)
        submitted = self.client.post("/api/v1/staff/me/onboarding/", {"personal": details(), "payment": {**BANK, "accountName": "Zainab Musa"}}, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.json())
        self.assertEqual(self.profile(staff_id)["onboardingStatus"], "submitted")
        self.assertEqual(Membership.objects.get(user=user, school=self.school).role, "teacher")
        self.assertTrue(Notification.objects.filter(recipient=self.owner, kind="staff_invitation_accepted").exists())

    def test_a_link_cannot_be_used_to_reach_anyone_elses_record(self):
        first_id, first_token = self.staff_with_link(email="one@school.ng", systemRole="teacher")
        second_id, second_token = self.staff_with_link(email="two@school.ng", systemRole="teacher")
        self.accept(first_token, self.new_person_body(firstName="One", lastName="Person"))
        user = User.objects.get(email="one@school.ng")
        self.client.force_authenticate(user)
        response = self.client.post("/api/v1/staff/me/onboarding/", {"personal": details(), "payment": BANK}, format="json")
        self.assertEqual(response.json()["staffId"], first_id)
        self.assertEqual(self.profile(second_id)["onboardingStatus"], "invitePending")

    def test_the_same_person_can_be_appointed_a_second_role_at_the_same_school(self):
        """A 'director' appointment: someone already linked as staff is proposed and
        approved again under a second, separate staff record for the extra role, and
        accepting that invitation while signed in as their existing account adds a
        second real Membership - it does not touch or replace the first."""
        staff_id, token = self.staff_with_link(
            email="director@school.ng", systemRole="staff", name="Peter James"
        )
        self.accept(token, self.new_person_body(firstName="Peter", lastName="James"))
        user = User.objects.get(email="director@school.ng")
        self.assertEqual(Membership.objects.get(user=user, school=self.school).role, "staff")

        director_id, director_token = self.staff_with_link(
            email="director@school.ng", systemRole="administrator", name="Peter James (Director)"
        )
        self.assertNotEqual(director_id, staff_id)

        accepted = self.accept(director_token, {}, user=user)
        self.assertEqual(accepted.status_code, 200, accepted.json())

        memberships = Membership.objects.filter(user=user, school=self.school)
        self.assertEqual(set(memberships.values_list("role", flat=True)), {"staff", "administrator"})
        # The original staff link and membership are untouched, not replaced.
        self.assertEqual(StaffLink.objects.get(school=self.school, staff_id=staff_id).membership.role, "staff")
        self.assertEqual(
            StaffLink.objects.get(school=self.school, staff_id=director_id).membership.role, "administrator"
        )
