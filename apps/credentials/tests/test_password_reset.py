import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import LoginIdentity, LoginIdentityKind
from apps.schools.models import Membership, Role, School

from ..models import CredentialAuditEvent

User = get_user_model()
PASSWORD = "a-long-test-password-1"
NEW_PASSWORD = "a-different-long-password-2"

REQUEST_URL = "/api/v1/credentials/password-reset/request/"
CONFIRM_URL = "/api/v1/credentials/password-reset/confirm/"
CODE_RE = re.compile(r"reset code is: (\d{6})")


class PasswordResetTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.school = School.objects.create(name="BrightGate Academy", slug="brightgate")
        self.user = User.objects.create_user(
            "owner@school.ng", PASSWORD, first_name="Ibrahim"
        )
        self.membership = Membership.objects.create(
            user=self.user, school=self.school, role=Role.PROPRIETOR
        )
        mail.outbox.clear()

    def request(self, email="owner@school.ng"):
        return self.client.post(REQUEST_URL, {"email": email}, format="json")

    def confirm(self, email="owner@school.ng", code=None, new_password=NEW_PASSWORD):
        if code is None:
            code = self.code_from_last_email()
        return self.client.post(
            CONFIRM_URL,
            {"email": email, "code": code, "newPassword": new_password},
            format="json",
        )

    def code_from_last_email(self):
        match = CODE_RE.search(mail.outbox[-1].body)
        assert match, "no reset code found in the last sent email"
        return match.group(1)

    def sign_in(self, email="owner@school.ng", password=NEW_PASSWORD):
        return self.client.post(
            "/api/v1/auth/token/", {"email": email, "password": password}, format="json"
        )

    # -- the request step never discloses anything --------------------------

    def test_request_for_a_real_account_sends_one_email_and_a_generic_response(self):
        response = self.request()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["accepted"], True)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["owner@school.ng"])

    def test_request_for_an_unknown_email_gives_the_same_response_and_sends_nothing(self):
        known = self.request()
        unknown = self.request(email="nobody@school.ng")
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.json(), unknown.json())
        self.assertEqual(len(mail.outbox), 1)  # only the real account's email

    def test_request_is_case_and_whitespace_insensitive(self):
        response = self.request(email="  Owner@School.NG  ")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(mail.outbox), 1)

    def test_a_student_or_parent_login_identity_is_never_eligible_here(self):
        # The account technically carries an email, but its real login identity
        # is a Student admission number: recovery for it stays school-office-only.
        LoginIdentity.objects.create(
            user=self.user,
            kind=LoginIdentityKind.STUDENT_ADMISSION,
            identifier="ADM-001",
            normalized_identifier="adm-001",
        )
        response = self.request()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(mail.outbox), 0)

    def test_an_inactive_account_gets_the_same_response_and_no_email(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        response = self.request()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_fresh_proprietor_with_no_school_membership_yet_is_still_eligible(self):
        User.objects.create_user("fresh@school.ng", PASSWORD, first_name="Amina")
        response = self.request(email="fresh@school.ng")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(mail.outbox), 1)

    def test_requesting_again_within_the_cooldown_sends_only_one_email(self):
        self.request()
        self.request()
        self.assertEqual(len(mail.outbox), 1)

    def test_requesting_logs_an_audit_event_for_every_active_school(self):
        self.request()
        events = CredentialAuditEvent.objects.filter(user=self.user, event="password_reset_requested")
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().school_id, self.school.id)

    # -- confirming resets the password and revokes old sessions ------------

    def test_the_right_code_resets_the_password_and_signs_in_works_after(self):
        self.request()
        before_version = self.user.credential_version
        response = self.confirm()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reset"], True)

        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password(PASSWORD))
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertFalse(self.user.must_change_password)
        self.assertEqual(self.user.credential_version, before_version + 1)
        self.assertEqual(self.sign_in().status_code, 200)

    def test_a_used_code_cannot_be_replayed(self):
        self.request()
        code = self.code_from_last_email()
        first = self.confirm(code=code)
        self.assertEqual(first.status_code, 200)
        second = self.confirm(code=code, new_password="yet-another-long-password-3")
        self.assertEqual(second.status_code, 400)

    def test_confirming_logs_an_audit_event(self):
        self.request()
        self.confirm()
        events = CredentialAuditEvent.objects.filter(user=self.user, event="password_reset_completed")
        self.assertEqual(events.count(), 1)

    def test_a_wrong_code_is_refused_and_counts_against_the_attempt_limit(self):
        self.request()
        response = self.confirm(code="000000")
        self.assertEqual(response.status_code, 400)
        self.assertIn("4 attempts", response.json()["message"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.password_reset_attempts, 1)
        # The real code still works afterwards; only wrong guesses are spent.
        self.assertEqual(self.confirm().status_code, 200)

    def test_five_wrong_codes_burn_the_real_one_too(self):
        self.request()
        code = self.code_from_last_email()
        for _ in range(5):
            self.confirm(code="000000")
        locked_out = self.confirm(code=code)
        self.assertEqual(locked_out.status_code, 400)
        self.assertIn("Too many", locked_out.json()["message"])

    def test_an_expired_code_is_refused(self):
        self.request()
        self.user.refresh_from_db()
        self.user.password_reset_expires_at = timezone.now() - timezone.timedelta(minutes=1)
        self.user.save(update_fields=["password_reset_expires_at"])
        response = self.confirm()
        self.assertEqual(response.status_code, 400)

    def test_a_weak_new_password_is_refused_but_the_code_is_not_burned(self):
        self.request()
        code = self.code_from_last_email()
        weak = self.confirm(code=code, new_password="short")
        self.assertEqual(weak.status_code, 400)
        self.assertIn("newPassword", weak.json())
        # The code itself was correct, so trying again with a real password still works.
        strong = self.confirm(code=code)
        self.assertEqual(strong.status_code, 200)

    def test_confirming_without_ever_requesting_is_refused(self):
        response = self.confirm(code="123456")
        self.assertEqual(response.status_code, 400)

    def test_a_student_or_parent_account_cannot_be_reset_through_this_endpoint(self):
        LoginIdentity.objects.create(
            user=self.user,
            kind=LoginIdentityKind.STUDENT_ADMISSION,
            identifier="ADM-002",
            normalized_identifier="adm-002",
        )
        self.user.password_reset_code_hash = "irrelevant"
        self.user.password_reset_expires_at = timezone.now() + timezone.timedelta(minutes=5)
        self.user.save(update_fields=["password_reset_code_hash", "password_reset_expires_at"])
        response = self.confirm(code="123456")
        self.assertEqual(response.status_code, 400)
