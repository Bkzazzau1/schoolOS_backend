"""AcademicClassHandler: an Administrator may create or edit a class (e.g. "1A",
"1B") for any section, whole-school. A Principal may do the same, but - like every
other curriculum/teaching-assignment write (see test_curriculum_stage_scope.py) -
only for their own Secondary; anyone else (Accountant, Teacher, ...) may not write
at all, by default."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.academics.handlers import AcademicClassHandler
from apps.core.errors import Rejected
from apps.schools.models import Membership, Role, School
from apps.sync import registry

User = get_user_model()
PASSWORD = "a-long-test-password-1"


class AcademicClassHandlerTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="BrightGate", slug="brightgate-class-handler")
        self.handler = AcademicClassHandler()

    def _member(self, role, email):
        return Membership.objects.create(user=User.objects.create_user(email, PASSWORD), school=self.school, role=role)

    def _payload(self, *, id="C1", section="Secondary", **over):
        return {
            "id": id,
            "code": "1A",
            "name": "1A",
            "section": section,
            "levelOrder": 1,
            "isTerminal": False,
            "isActive": True,
            **over,
        }

    def _clean(self, membership, payload):
        ctx = registry.MutationContext(
            membership=membership, operation="create", entity_type="academic_class", entity_id=payload["id"],
            payload=payload, existing=None, now="2026-09-21T00:00:00+00:00",
        )
        return self.handler.clean(ctx)

    def test_an_administrator_may_create_a_class_in_any_section(self):
        admin = self._member(Role.ADMINISTRATOR, "admin@brightgate.ng")
        self.handler.authorize(registry.MutationContext(
            membership=admin, operation="create", entity_type="academic_class", entity_id="C1",
            payload={}, existing=None, now="2026-09-21T00:00:00+00:00",
        ))  # does not raise
        for section in ("Secondary", "Primary", "Nursery"):
            cleaned = self._clean(admin, self._payload(section=section))
            self.assertEqual(cleaned["section"], section)

    def test_a_principal_may_create_or_edit_only_a_secondary_class(self):
        principal = self._member(Role.PRINCIPAL, "principal@brightgate.ng")
        cleaned = self._clean(principal, self._payload(section="Secondary"))
        self.assertEqual(cleaned["section"], "Secondary")
        for section in ("Primary", "Nursery"):
            with self.assertRaises(Rejected):
                self._clean(principal, self._payload(section=section))

    def test_an_accountant_may_not_write_classes_at_all(self):
        accountant = self._member(Role.ACCOUNTANT, "accountant@brightgate.ng")
        with self.assertRaises(Rejected):
            self.handler.authorize(registry.MutationContext(
                membership=accountant, operation="create", entity_type="academic_class", entity_id="C1",
                payload={}, existing=None, now="2026-09-21T00:00:00+00:00",
            ))

    def test_a_principal_may_read_any_section_despite_the_write_scope(self):
        principal = self._member(Role.PRINCIPAL, "principal2@brightgate.ng")
        payload = self._payload(section="Primary")
        self.assertEqual(self.handler.visible(principal, payload), payload)
