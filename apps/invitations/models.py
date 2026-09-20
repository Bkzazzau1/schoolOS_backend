import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.schools.models import Membership, School


class StaffInvitation(models.Model):
    """"This email may claim this staff record at this school, once, before this time."

    Only a hash of the link's secret is stored, so a copy of the database cannot be
    used to accept anyone's invitation. A staff record has at most one pending
    invitation: sending a new one revokes the old.
    """

    class Status(models.TextChoices):
        PENDING = "pending"
        ACCEPTED = "accepted"
        REVOKED = "revoked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="staff_invitations")
    #: The STAFF-... id of the staff record. Not a foreign key: staff records are
    #: synced JSON, not tables.
    staff_id = models.CharField(max_length=128)
    email = models.EmailField()
    #: The access role the person is given on accepting. Fixed when the invitation
    #: is made (from the approved proposal); the person never chooses it.
    role = models.CharField(max_length=20)
    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    expires_at = models.DateTimeField()
    created_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    #: When the mail server accepted the message. Not proof it was read.
    sent_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_membership = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "staff_id"], condition=Q(status="pending"), name="one_pending_invitation_per_staff"
            )
        ]
        indexes = [models.Index(fields=["school", "staff_id"])]

    def __str__(self):
        return f"{self.staff_id} <{self.email}> {self.status}"

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()

    @property
    def is_usable(self) -> bool:
        return self.status == self.Status.PENDING and not self.is_expired


class StaffLink(models.Model):
    """Which login is which staff member.

    One staff record has at most one login, and one login has at most one staff
    record, and the database enforces both. This is what makes "only the staff
    member can change their own bank details" true: the profile's
    `linkedMembershipId` is set from here, by the server, never by the app.
    """

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="staff_links")
    staff_id = models.CharField(max_length=128)
    membership = models.OneToOneField(Membership, on_delete=models.CASCADE, related_name="staff_link")
    linked_at = models.DateTimeField(auto_now_add=True)
    linked_via = models.ForeignKey(StaffInvitation, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["school", "staff_id"], name="one_login_per_staff_record")]

    def __str__(self):
        return f"{self.staff_id} <- {self.membership_id}"


class InvitationEvent(models.Model):
    """Everything that happened to an invitation. Append-only. Never holds the link."""

    invitation = models.ForeignKey(StaffInvitation, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(auto_now_add=True)
    event = models.CharField(max_length=20)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=200, blank=True)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["at", "id"]
