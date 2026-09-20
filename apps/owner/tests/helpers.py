from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.schools.models import Membership, Role, School
from apps.sync.models import SyncRecord

User = get_user_model()
PASSWORD = "a-long-test-password-1"


class OwnerTestCase(APITestCase):
    """A school with an owner and one person in every other role."""

    entity_type = ""

    def setUp(self):
        self.school = School.objects.create(name="BrightGate", slug="brightgate")
        self.members = {}
        for role in Role:
            user = User.objects.create_user(f"{role.value}@school.ng", PASSWORD)
            self.members[role.value] = Membership.objects.create(
                user=user, school=self.school, role=role
            )
        self.owner = self.members["proprietor"]
        self._n = 0

    def push(self, entity_id, payload=None, *, operation="create", who=None,
             base_version=None, entity_type=None, school=None):
        who = who or self.owner
        self._n += 1
        self.client.force_authenticate(who.user)
        body = {
            "id": f"m{self._n}",
            "tenantId": str((school or self.school).id),
            "membershipId": str(who.id),
            "entityType": entity_type or self.entity_type,
            "entityId": entity_id,
            "operation": operation,
        }
        if operation != "delete":
            body["payload"] = payload
        if base_version is not None:
            body["baseVersion"] = base_version
        return self.client.post("/api/v1/sync/push/", body, format="json")

    def stored(self, entity_id, entity_type=None):
        return SyncRecord.objects.get(
            school=self.school, entity_type=entity_type or self.entity_type, entity_id=entity_id
        )

    def assertAccepted(self, response):
        self.assertEqual(response.status_code, 200, response.json())

    def assertRejected(self, response, contains=None):
        self.assertEqual(response.status_code, 422, response.json())
        if contains:
            self.assertIn(contains, response.json()["message"])
