import re

from django.core import mail
from django.test import override_settings

from apps.invitations import service
from apps.invitations.listeners import give_login_when_registration_is_requested
from apps.invitations.models import StaffInvitation
from apps.staff.signals import registration_requested
from apps.staff.tests.helpers import StaffTestCase

LINK = re.compile(r"https?://([^/\s]+)/invite/([^/\s]+)/")


@override_settings(PLATFORM_DOMAIN="schoolos.ng", ALLOWED_HOSTS=["*"])
class InviteTestCase(StaffTestCase):
    """Staff tests plus a school with a web address and an official email."""

    def setUp(self):
        super().setUp()
        self.school.official_email = "office@brightgate.schoolos.ng"
        self.school.official_sender_name = "BrightGate Academy Office"
        self.school.save()
        mail.outbox.clear()

    # -- making invitations ------------------------------------------------------------

    def make_staff_no_bootstrap(self, **over):
        """A real staff member, approved the way the app does it, with the automatic
        phone-number bootstrap login (now the default on approval - see
        apps.invitations.accept.provision_staff_login) suppressed. Lets a test decide
        for itself, via new_link(), when and how a login gets attached - the same
        shape a real "email already belongs to someone" or owner-initiated resend
        would leave the staff record in. Returns staff_id."""
        registration_requested.disconnect(give_login_when_registration_is_requested)
        try:
            staff_id = self.make_staff(**over)
        finally:
            registration_requested.connect(give_login_when_registration_is_requested)
        return staff_id

    def staff_with_link(self, **over):
        """A real staff member, approved the way the app does it, and the link that
        was emailed to them - the automatic bootstrap is suppressed (see
        make_staff_no_bootstrap) so this keeps testing the token/link mechanism
        itself, the same as before phone-bootstrap became the default outcome of an
        ordinary approval. Returns (staff_id, token)."""
        staff_id = self.make_staff_no_bootstrap(**over)
        # new_link() already wraps its own send in captureOnCommitCallbacks - nesting
        # another one here double-sends.
        self.new_link(staff_id, email=over.get("email", "musa@school.ng"), role=over.get("systemRole", "teacher"))
        return staff_id, self.token_from_mail()

    def token_from_mail(self, message=-1):
        found = LINK.search(mail.outbox[message].body)
        self.assertIsNotNone(found, mail.outbox[message].body)
        return found.group(2)

    def new_link(self, staff_id, email="musa@school.ng", role="staff", by=None):
        """Send a fresh invitation (the older one stops working). Returns the token."""
        with self.captureOnCommitCallbacks(execute=True):
            _, token = service.create_invitation(self.school, staff_id, email, role, created_by=by)
        return token

    def pending(self, staff_id):
        return StaffInvitation.objects.filter(school=self.school, staff_id=staff_id, status="pending").first()

    # -- calling the public API -----------------------------------------------------------

    def preview(self, token, host=None):
        self.client.force_authenticate(None)
        extra = {"HTTP_HOST": host} if host else {}
        return self.client.get(f"/api/v1/invitations/{token}/", **extra)

    def accept(self, token, body=None, user=None, host=None):
        self.client.force_authenticate(user)
        extra = {"HTTP_HOST": host} if host else {}
        return self.client.post(f"/api/v1/invitations/{token}/accept/", body or {}, format="json", **extra)

    def new_person_body(self, **over):
        body = {"password": "a-good-long-password-1", "firstName": "Musa", "lastName": "Ibrahim"}
        body.update(over)
        return body
