import re

from django.core import mail
from django.test import override_settings

from apps.invitations import service
from apps.invitations.models import StaffInvitation
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

    def staff_with_link(self, **over):
        """A real staff member, approved the way the app does it, and the link that
        was emailed to them. Returns (staff_id, token)."""
        with self.captureOnCommitCallbacks(execute=True):
            staff_id = self.make_staff(**over)
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
