import copy
import itertools

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.schools.models import Membership, Role, School
from apps.sync.models import SyncRecord

User = get_user_model()
PASSWORD = "a-long-test-password-1"
_numbers = itertools.count(1)


def fresh_phone() -> str:
    return f"0803{next(_numbers):07d}"


def fresh_nin() -> str:
    return f"{next(_numbers):011d}"


class StaffTestCase(APITestCase):
    """A school with one person in every role, and a second, separate school."""

    def setUp(self):
        self.school = School.objects.create(name="BrightGate", slug="brightgate")
        self.other_school = School.objects.create(name="Other", slug="other")
        self.members = {}
        for role in Role:
            user = User.objects.create_user(f"{role.value}@school.ng", PASSWORD)
            self.members[role.value] = Membership.objects.create(user=user, school=self.school, role=role)
        self.owner = self.members["proprietor"]
        other_user = User.objects.create_user("owner@other.ng", PASSWORD)
        self.other_owner = Membership.objects.create(user=other_user, school=self.other_school, role=Role.PROPRIETOR)
        self.other_principal = Membership.objects.create(
            user=User.objects.create_user("principal@other.ng", PASSWORD), school=self.other_school, role=Role.PRINCIPAL
        )
        self._mutation = 0

    # -- sync ------------------------------------------------------------------------

    def push(self, entity_type, entity_id, payload=None, *, operation="create", who=None,
             base_version=None, school=None):
        who = who or self.owner
        self._mutation += 1
        self.client.force_authenticate(who.user)
        body = {
            "id": f"m{self._mutation}", "tenantId": str((school or self.school).id),
            "membershipId": str(who.id), "entityType": entity_type, "entityId": entity_id,
            "operation": operation,
        }
        if operation != "delete":
            body["payload"] = payload
        if base_version is not None:
            body["baseVersion"] = base_version
        return self.client.post("/api/v1/sync/push/", body, format="json")

    def ok(self, response):
        self.assertEqual(response.status_code, 200, response.json())

    def rejected(self, response, contains=None):
        self.assertEqual(response.status_code, 422, response.json())
        if contains:
            self.assertIn(contains, response.json()["message"])

    def stored(self, entity_type, entity_id, school=None):
        return SyncRecord.objects.get(school=school or self.school, entity_type=entity_type, entity_id=entity_id)

    # -- proposals -------------------------------------------------------------------

    def proposal_payload(self, **over):
        payload = {
            "name": "Musa Ibrahim", "roleTitle": "Mathematics Teacher", "systemRole": "teacher",
            "workArea": "Secondary", "email": "musa@school.ng", "phone": fresh_phone(),
            "nin": fresh_nin(), "gross": 200000, "deductions": 20000,
        }
        payload.update(over)
        return payload

    def propose(self, who=None, proposal_id=None, **over):
        proposal_id = proposal_id or f"PROP-{next(_numbers)}"
        response = self.push("staff_proposal", proposal_id, self.proposal_payload(**over), who=who or self.members["principal"])
        self.last_proposal_id = proposal_id
        return response

    def decide(self, action, who, proposal_id, body=None, school=None):
        self.client.force_authenticate(who.user if who else None)
        return self.client.post(
            f"/api/v1/staff/schools/{(school or self.school).id}/proposals/{proposal_id}/{action}/",
            body or {}, format="json",
        )

    def approve(self, who, proposal_id, body=None):
        return self.decide("approve", who, proposal_id, body)

    def reject(self, who, proposal_id, note=""):
        return self.decide("reject", who, proposal_id, {"note": note})

    def make_staff(self, **over):
        """A real staff member, made the way the app makes them: proposed, then approved."""
        self.ok(self.propose(**over))
        response = self.approve(self.owner, self.last_proposal_id)
        self.assertEqual(response.status_code, 200, response.json())
        return response.json()["staffId"]

    def assign_approver(self, member, *, authorities=("approveStaff",), status="active", linked_to=None):
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_payroll_authorizer", entity_id=f"AUTH-{member.id}",
            payload={
                "staffId": f"AUTH-{member.id}", "name": member.role, "authorities": list(authorities),
                "status": status, "membershipId": str(linked_to.id if linked_to else member.id),
            },
        )

    # -- profiles --------------------------------------------------------------------

    def profile(self, staff_id):
        return copy.deepcopy(self.stored("owner_staff_profile", staff_id).payload)

    def edit_profile(self, staff_id, who, **sections):
        """Change some sections of the stored profile and push it, as the app does."""
        payload = self.profile(staff_id)
        for key, value in sections.items():
            if isinstance(value, dict) and isinstance(payload.get(key), dict):
                payload[key].update(value)
            else:
                payload[key] = value
        return self.push("owner_staff_profile", staff_id, payload, operation="update", who=who)

    def link(self, staff_id, membership):
        """What accepting an invitation will do: tie this login to the staff record."""
        record = self.stored("owner_staff_profile", staff_id)
        record.payload = {**record.payload, "linkedMembershipId": str(membership.id)}
        record.save()
