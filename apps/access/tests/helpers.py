from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.schools.models import Membership, Role, School

User = get_user_model()
PASSWORD = "a-long-test-password-1"


class AccessTestCase(APITestCase):
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
        self.other_teacher = Membership.objects.create(
            user=User.objects.create_user("teacher@other.ng", PASSWORD), school=self.other_school, role=Role.TEACHER
        )

    # -- calling the API ---------------------------------------------------------

    def call(self, method, path, who=None, data=None):
        self.client.force_authenticate(who.user if who else None)
        return getattr(self.client, method)(f"/api/v1/{path}", data, format="json")

    def owner_path(self, tail, school=None):
        return f"owner/schools/{(school or self.school).id}/access/{tail}"

    def me(self, who):
        return self.call("get", f"schools/{who.school_id}/access/me/", who)

    def my_activities(self, who):
        return set(self.me(who).json()["activities"])

    def person_path(self, member, activity):
        return self.owner_path(f"people/{member.id}/activities/{activity}/")
