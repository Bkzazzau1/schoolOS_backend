from datetime import timedelta
from unittest import mock

from django.core import mail
from django.db import transaction
from django.test import override_settings
from django.utils import timezone

from apps.invitations import service
from apps.invitations.models import InvitationEvent, StaffInvitation, StaffLink
from apps.invitations.service import InvitationError
from apps.invitations.tokens import hash_token, mask_email, new_token

from .helpers import LINK, InviteTestCase


class TokenTests(InviteTestCase):
    def test_tokens_are_long_unique_and_url_safe(self):
        tokens = {new_token() for _ in range(200)}
        self.assertEqual(len(tokens), 200)
        self.assertTrue(all(len(t) >= 43 and all(c.isalnum() or c in "-_" for c in t) for t in tokens))

    def test_only_a_hash_is_ever_stored(self):
        staff_id, token = self.staff_with_link()
        invitation = self.pending(staff_id)
        self.assertEqual(invitation.token_hash, hash_token(token))
        self.assertNotEqual(invitation.token_hash, token)
        self.assertEqual(len(invitation.token_hash), 64)
        # The secret is nowhere in the database: not on the invitation, not in its events.
        stored = " ".join(str(v) for v in invitation.__dict__.values())
        stored += " ".join(str(e.detail) + e.event for e in invitation.events.all())
        self.assertNotIn(token, stored)

    def test_masking_an_email(self):
        self.assertEqual(mask_email("musa@school.ng"), "m***@school.ng")
        self.assertEqual(mask_email("a@b.ng"), "a***@b.ng")


class CreatingInvitationsTests(InviteTestCase):
    def setUp(self):
        super().setUp()
        self.staff_id = self.make_staff()

    def test_an_invitation_lasts_the_configured_time_and_records_who_and_when(self):
        with override_settings(INVITATION_TTL_DAYS=3):
            invitation, _ = service.create_invitation(self.school, self.staff_id, "Musa@School.NG", "teacher", created_by=self.owner)
        self.assertEqual((invitation.email, invitation.role, invitation.status), ("musa@school.ng", "teacher", "pending"))
        self.assertAlmostEqual((invitation.expires_at - timezone.now()).total_seconds(), 3 * 86400, delta=30)
        self.assertEqual(invitation.created_by, self.owner)
        self.assertEqual(list(invitation.events.values_list("event", flat=True)), ["created"])

    def test_sending_again_replaces_the_old_link(self):
        first = self.pending(self.staff_id)
        service.create_invitation(self.school, self.staff_id, "musa@school.ng", "staff")
        first.refresh_from_db()
        self.assertEqual(first.status, "revoked")
        self.assertEqual(StaffInvitation.objects.filter(staff_id=self.staff_id, status="pending").count(), 1)
        self.assertIn("revoked", list(first.events.values_list("event", flat=True)))

    def test_the_database_allows_only_one_pending_invitation_per_staff_record(self):
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError), transaction.atomic():
            StaffInvitation.objects.create(
                school=self.school, staff_id=self.staff_id, email="x@y.ng", role="staff",
                token_hash="0" * 64, expires_at=timezone.now() + timedelta(days=1),
            )

    def test_nothing_is_sent_to_someone_who_already_has_a_login(self):
        StaffLink.objects.create(school=self.school, staff_id=self.staff_id, membership=self.members["staff"])
        with self.assertRaises(InvitationError) as caught:
            service.create_invitation(self.school, self.staff_id, "musa@school.ng", "staff")
        self.assertEqual((caught.exception.code, caught.exception.status), ("already_linked", 409))

    def test_a_bad_email_is_refused(self):
        for email in ["", "nope", "a@b", "a b@c.ng"]:
            with self.assertRaises(InvitationError, msg=email) as caught:
                service.create_invitation(self.school, self.staff_id, email, "staff")
            self.assertEqual(caught.exception.code, "invalid_email")

    def test_describe_says_where_it_stands_and_never_shows_the_link(self):
        self.assertEqual(service.describe(self.school, "STAFF-NONE"), {"status": "none", "linked": False})
        invitation = self.pending(self.staff_id)
        info = service.describe(self.school, self.staff_id)
        self.assertEqual((info["status"], info["email"], info["linked"]), ("pending", "musa@school.ng", False))
        self.assertNotIn("token", str(info).lower())
        invitation.expires_at = timezone.now() - timedelta(seconds=1)
        invitation.save()
        self.assertEqual(service.describe(self.school, self.staff_id)["status"], "expired")


class EmailTests(InviteTestCase):
    def test_the_email_is_sent_once_the_change_is_saved_not_before(self):
        staff_id = self.make_staff()
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            service.create_invitation(self.school, staff_id, "musa@school.ng", "teacher")
        self.assertEqual(len(mail.outbox), 0)          # not yet: the transaction has not committed
        for callback in callbacks:
            callback()
        self.assertEqual(len(mail.outbox), 1)

    def test_a_rolled_back_change_sends_nothing(self):
        staff_id = self.make_staff()
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    service.create_invitation(self.school, staff_id, "musa@school.ng", "staff")
                    raise RuntimeError("something later failed")
            except RuntimeError:
                pass
        self.assertEqual(len(mail.outbox), 0)

    def test_it_comes_from_the_schools_official_address_to_the_person(self):
        staff_id, token = self.staff_with_link(email="musa@school.ng")
        message = mail.outbox[-1]
        self.assertEqual(message.from_email, '"BrightGate Academy Office" <office@brightgate.schoolos.ng>')
        self.assertEqual(message.to, ["musa@school.ng"])
        self.assertIn("BrightGate", message.subject)

    def test_the_link_uses_the_schools_own_web_address(self):
        _, token = self.staff_with_link()
        host, found = LINK.search(mail.outbox[-1].body).groups()
        self.assertEqual((host, found), ("brightgate.schoolos.ng", token))

    def test_it_says_who_what_role_and_until_when_and_nothing_private(self):
        staff_id, token = self.staff_with_link(name="Musa Ibrahim", systemRole="teacher", gross=345678,
                                               nin="12345678901", phone="08031234567")
        body = mail.outbox[-1].body
        for expected in ["Musa Ibrahim", "BrightGate", "Teacher", "ignore this email"]:
            self.assertIn(expected, body)
        expires = timezone.localtime(self.pending(staff_id).expires_at).strftime("%d %B %Y")
        self.assertIn(expires, body)
        for private in ["345678", "12345678901", "08031234567", "Mathematics"]:
            self.assertNotIn(private, body)

    def test_without_an_official_address_the_platform_sender_is_used(self):
        self.school.official_email = ""
        self.school.save()
        self.staff_with_link()
        self.assertEqual(mail.outbox[-1].from_email, "SchoolOS <no-reply@localhost>")

    def test_a_sent_email_is_recorded(self):
        staff_id, _ = self.staff_with_link()
        invitation = self.pending(staff_id)
        self.assertIsNotNone(invitation.sent_at)
        self.assertEqual(list(invitation.events.values_list("event", flat=True)), ["created", "sent"])


class WhenEmailCannotBeSentTests(InviteTestCase):
    def setUp(self):
        super().setUp()
        self.staff_id = self.make_staff()

    def last_events(self):
        return list(self.pending(self.staff_id).events.values_list("event", flat=True))

    def test_a_failed_send_is_recorded_without_the_link_and_the_invitation_survives(self):
        with mock.patch("apps.invitations.mail.EmailMultiAlternatives.send", side_effect=ConnectionError("smtp down")):
            token = self.new_link(self.staff_id)
        invitation = self.pending(self.staff_id)
        self.assertIsNone(invitation.sent_at)
        failed = invitation.events.get(event="failed")
        self.assertEqual(failed.detail["error"], "ConnectionError")
        self.assertNotIn(token, str(failed.detail))
        # The owner can see it and send again.
        self.assertEqual(service.describe(self.school, self.staff_id)["lastEvent"]["event"], "failed")
        self.new_link(self.staff_id)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(EMAIL_BACKEND="apps.invitations.mail.NotConfiguredBackend")
    def test_production_without_email_configured_fails_loudly_instead_of_silently(self):
        self.new_link(self.staff_id)
        failed = self.pending(self.staff_id).events.get(event="failed")
        self.assertEqual(failed.detail["error"], "ImproperlyConfigured")
        self.assertIn("EMAIL_URL", failed.detail["message"])

    @override_settings(PLATFORM_DOMAIN="", INVITATION_FALLBACK_HOST="")
    def test_a_school_with_no_web_address_cannot_be_emailed_a_link_and_it_says_so(self):
        from apps.domains.models import SchoolDomain

        SchoolDomain.objects.all().delete()
        self.new_link(self.staff_id)
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("no web address", self.pending(self.staff_id).events.get(event="failed").detail["message"])

    @override_settings(INVITATION_FALLBACK_HOST="join.schoolos.ng")
    def test_a_school_without_its_own_address_can_use_the_fallback_host(self):
        from apps.domains.models import SchoolDomain

        SchoolDomain.objects.all().delete()
        self.new_link(self.staff_id)
        self.assertIn("//join.schoolos.ng/invite/", mail.outbox[-1].body)

    def test_a_revoked_invitation_is_never_emailed(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            service.create_invitation(self.school, self.staff_id, "musa@school.ng", "staff")
        service.revoke(self.school, self.staff_id)
        mail.outbox.clear()
        for callback in callbacks:
            callback()
        self.assertEqual(len(mail.outbox), 0)
